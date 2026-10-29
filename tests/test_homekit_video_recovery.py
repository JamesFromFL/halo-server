"""Absolute source deadlines and persistent dependency recovery, synthetic only."""

import asyncio
import struct
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest
from test_homekit_live import (
    CONFIG,
    encoded_picture,
    native_crypto,
    observe_video_clock,
    prepared,
    reconstruct,
    wire_idr,
)
from test_media_h264 import encode_source
from test_video_wire_encoder import encode_moving_source

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media import homekit_live as module
from halo_server.media.buffer import MediaStreamKind
from halo_server.media.rtp import RtpError
from halo_server.media.transcode import VideoWorker


@pytest.fixture(scope="module", params=(15, 30))
def sender_motion(request):
    return request.param, encode_moving_source(request.param)


class Clock:
    def __init__(self):
        self.now = 100.0
        self.overshoot = 0
        self.after_sleep = None

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds + self.overshoot
        if self.after_sleep:
            self.after_sleep()
        await asyncio.sleep(0)


class AsyncioProxy:
    def __init__(self, clock):
        self.sleep = clock.sleep

    def __getattr__(self, name):
        return getattr(asyncio, name)


class Sink:
    def __init__(self, clock, owner):
        self.clock = clock
        self.owner = owner
        self.sent = []
        self.before = None

    def send(self, packet):
        if self.before:
            self.before()
        self.sent.append((self.clock(), packet))
        return len(packet)

    def fileno(self):
        return self.owner.fileno()

    def close(self):
        self.owner.close()


@asynccontextmanager
async def controlled(monkeypatch, *, bitrate=300):
    manager, controller, identity, _, receiver = await prepared()
    manager._reaper.cancel()
    await asyncio.gather(manager._reaper, return_exceptions=True)
    session = manager._sessions[identity]
    original_socket = session.socket
    clock = Clock()
    sink = Sink(clock, original_socket)
    manager._clock = clock
    session.handle, session.config = controller.handle, replace(CONFIG, max_bit_rate=bitrate)
    session.capture_epoch = controller.handle.capture_epoch
    session.last_source = session.last_renewal = clock()
    session.socket = sink
    observe_video_clock(manager, session)
    original_asyncio = module.asyncio
    monkeypatch.setattr(module, "asyncio", AsyncioProxy(clock))
    try:
        yield manager, controller, session, clock, sink
    finally:
        monkeypatch.setattr(module, "asyncio", original_asyncio)
        session.socket = original_socket
        receiver.close()
        await manager.close()


def output(payload=None, timestamp=0, *, keyframe=True):
    return NS(
        payload=encoded_picture() if payload is None else payload,
        timestamp=timestamp,
        is_keyframe=keyframe,
        mode="adapt",
    )


@pytest.mark.asyncio
async def test_predictable_whole_au_miss_rejects_before_any_wire_or_crypto_index(monkeypatch):
    async with controlled(monkeypatch, bitrate=132) as (manager, _, session, clock, sink):
        protects = []
        original = session.secure.protect
        session.secure.protect = lambda packet: protects.append(packet) or original(packet)
        sequence = session.packetizer.sequence
        assert not await manager._access_unit(
            session, output(wire_idr(150000)), session.capture_epoch, received_at=clock()
        )
        assert not sink.sent and not protects
        assert session.packetizer.sequence == sequence
        assert session.awaiting_idr and session.recovery_attempts == 1
        assert not session.retired


@pytest.mark.asyncio
async def test_pacing_oversleep_never_admits_a_stale_datagram(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        session.next_packet_at = clock() + 0.49
        clock.overshoot = 0.03
        with pytest.raises(module._VideoDeadline):
            await manager._paced_send(
                session, b"protected", epoch=session.capture_epoch, deadline=clock() + 0.5
            )
        assert not sink.sent


@pytest.mark.asyncio
async def test_small_oversleep_rejects_virtual_service_overrun_before_source_deadline(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        session.next_packet_at = clock() + 0.490
        clock.overshoot = 0.007
        deadline = clock() + 0.500
        with pytest.raises(module._VideoDeadline):
            await manager._paced_send(
                session, b"p" * 1000, epoch=session.capture_epoch, deadline=deadline
            )
        assert clock() < deadline
        assert clock() + 1000 * 8 / 2000000 > deadline
        assert not sink.sent


@pytest.mark.asyncio
async def test_blocking_udp_retry_keeps_original_absolute_deadline(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):

        def busy():
            raise BlockingIOError

        sink.before = busy
        deadline = clock() + 0.003
        with pytest.raises(module._VideoDeadline):
            await manager._send(
                session, b"protected", epoch=session.capture_epoch, deadline=deadline
            )
        assert not sink.sent and clock() > deadline


@pytest.mark.asyncio
async def test_blocking_udp_retry_reserves_remaining_datagram_service_time(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):

        def busy():
            raise BlockingIOError

        sink.before = busy
        deadline = clock() + 0.030
        with pytest.raises(module._VideoDeadline):
            await manager._paced_send(
                session, b"p" * 1000, epoch=session.capture_epoch, deadline=deadline
            )
        assert not sink.sent and clock() < deadline
        assert clock() + 1000 * 8 / 2000000 > deadline


@pytest.mark.asyncio
async def test_partial_au_expiry_keeps_recovery_and_purges_partial_ciphertext(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        clock.overshoot = 0.55
        before_sequence = session.packetizer.sequence
        assert not await manager._access_unit(
            session, output(wire_idr(1000)), session.capture_epoch, received_at=clock()
        )
        assert len(sink.sent) == 1
        assert session.packetizer.sequence != before_sequence  # Never roll encrypted indexes back.
        assert session.awaiting_idr and not session.retransmit and not session.retransmit_bytes
        assert session.recovery_attempts == 1 and not session.retired


@pytest.mark.asyncio
async def test_stale_reference_and_multiple_p_outputs_stay_blocked_until_complete_idr(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        source = encode_source(frames=5, force=(3,))
        requests = []
        session.adapter = NS(
            request_keyframe=lambda: requests.append(True), close=lambda: asyncio.sleep(0)
        )
        manager._need_idr(session)
        for index in (1, 2):
            assert not await manager._access_unit(
                session,
                output(source[index][0], index * 6000, keyframe=False),
                session.capture_epoch,
                received_at=clock(),
            )
        assert not sink.sent and session.awaiting_idr
        assert await manager._access_unit(
            session, output(source[3][0], 18000), session.capture_epoch, received_at=clock()
        )
        assert not session.awaiting_idr and session.recovery_since is None
        assert await manager._access_unit(
            session,
            output(source[4][0], 24000, keyframe=False),
            session.capture_epoch,
            received_at=clock(),
        )
        assert requests == [True]


@pytest.mark.asyncio
async def test_keyframe_label_without_actual_idr_cannot_clear_recovery(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        source = encode_source(frames=2)
        assert not await manager._access_unit(
            session, output(source[1][0], keyframe=True), session.capture_epoch, received_at=clock()
        )
        assert session.awaiting_idr and not sink.sent
        with pytest.raises(RtpError, match="parameters"):
            await manager._access_unit(
                session, output(b"\0\0\1\x65\x88"), session.capture_epoch, received_at=clock()
            )
        assert session.awaiting_idr and not sink.sent


@pytest.mark.asyncio
async def test_fresh_recovery_idr_is_checked_after_every_wait(monkeypatch):
    async with controlled(monkeypatch) as (manager, controller, session, clock, sink):
        clock.after_sleep = controller.handle.buffer.clear
        with pytest.raises(module._SourceChanged):
            await manager._access_unit(
                session, output(), session.capture_epoch, received_at=clock()
            )
        assert session.awaiting_idr and not session.retransmit
        assert len(sink.sent) == 1


@pytest.mark.asyncio
async def test_infeasible_fresh_idrs_have_finite_attempts_but_p_skips_do_not_count(monkeypatch):
    async with controlled(monkeypatch, bitrate=132) as (manager, _, session, clock, sink):
        source = encode_source(frames=2)
        for index in range(module.MAX_RECOVERY_IDRS):
            if index == module.MAX_RECOVERY_IDRS - 1:
                with pytest.raises(HomeKitOperationError) as failure:
                    await manager._access_unit(
                        session,
                        output(wire_idr(150000)),
                        session.capture_epoch,
                        received_at=clock(),
                    )
                assert failure.value.code == "media_saturated"
            else:
                assert not await manager._access_unit(
                    session, output(wire_idr(150000)), session.capture_epoch, received_at=clock()
                )
                for _ in range(4):
                    assert not await manager._access_unit(
                        session, output(source[1][0]), session.capture_epoch, received_at=clock()
                    )
                assert session.recovery_attempts == index + 1
        assert not sink.sent


@pytest.mark.asyncio
async def test_recovery_elapsed_bound_and_startup_no_source_are_distinct(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, _):
        clock.now += 20
        manager._check_recovery(session)  # No candidate/source failure has begun recovery.
        manager._need_idr(session)
        began = session.recovery_since
        clock.now += module.RECOVERY_SECONDS - 0.001
        manager._need_idr(session)
        assert session.recovery_since == began  # Repeated damage cannot restart the budget.
        manager._check_recovery(session)
        clock.now += 0.002
        with pytest.raises(HomeKitOperationError) as failure:
            manager._check_recovery(session)
        assert failure.value.code == "media_saturated"


@pytest.mark.asyncio
async def test_whole_au_reservation_prevents_repairs_stealing_packet_slots(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        entered = asyncio.Event()
        release = asyncio.Event()
        original_sleep = clock.sleep

        async def hold(seconds):
            if not entered.is_set():
                entered.set()
                await release.wait()
            await original_sleep(seconds)

        module.asyncio.sleep = hold
        sending = asyncio.create_task(
            manager._access_unit(
                session, output(wire_idr(3000)), session.capture_epoch, received_at=clock()
            )
        )
        await entered.wait()
        repair = asyncio.create_task(
            manager._paced_send(
                session, b"repair", epoch=session.capture_epoch, deadline=clock() + 0.5
            )
        )
        await asyncio.sleep(0)
        assert not repair.done()
        release.set()
        assert await sending
        await repair
        assert sink.sent[-1][1] == b"repair"
        assert all(packet != b"repair" for _, packet in sink.sent[:-1])
        assert all(when <= 100.5 for when, _ in sink.sent)


@pytest.mark.asyncio
async def test_waiting_repair_cannot_send_ciphertext_removed_by_recovery(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        cached = (clock(), b"ciphertext", 10, session.capture_epoch, clock() + 0.5)
        session.retransmit[1] = cached
        session.retransmit_bytes = len(cached[1])
        await session.send_lock.acquire()
        repair = asyncio.create_task(
            manager._paced_send(
                session, cached[1], epoch=cached[3], deadline=cached[4], repair=(1, cached)
            )
        )
        await asyncio.sleep(0)
        manager._need_idr(session)
        session.send_lock.release()
        with pytest.raises(module._SourceChanged):
            await repair
        assert not sink.sent


@pytest.mark.asyncio
async def test_recovery_complete_actual_idr_still_independently_decodes_srtp(monkeypatch):
    import av

    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        crypto = native_crypto(session.prepared.ssrc)
        assert await manager._access_unit(
            session, output(), session.capture_epoch, received_at=clock()
        )
        clear = [crypto.unprotect(packet) for _, packet in sink.sent]
        assert clear[-1][1] & 0x80
        picture = b"".join(b"\0\0\0\1" + nal for nal in reconstruct(clear))
        decoded = av.CodecContext.create("h264", "r").decode(av.Packet(picture))
        assert len(decoded) == 1 and (decoded[0].width, decoded[0].height) == (320, 240)
        sequences = [struct.unpack_from("!H", packet, 2)[0] for packet in clear]
        assert len(sequences) == len(set(sequences))
        assert not session.awaiting_idr


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_idr", [False, True])
async def test_multioutput_feed_after_stale_reference_cannot_send_later_p(monkeypatch, stale_idr):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        source = encode_source(frames=3)
        calls = []
        requests = []
        if stale_idr:
            session.idr_requested = True  # A previous force is consumed by the stale IDR.

        async def feed(*_args, **_kwargs):
            calls.append(True)
            if len(calls) == 1:
                units = (output(source[0 if stale_idr else 1][0], 6000),)
            elif len(calls) == 2:
                units = (
                    output(source[1][0], 12000, keyframe=False),
                    output(source[2][0], 18000, keyframe=False),
                )
            else:
                units = (output(source[0][0], 24000),)
            for unit in units:
                unit.capture_epoch = _kwargs["capture_epoch"]
                unit.configuration_generation = 1
                unit.received_at = _kwargs["received_at"]
            return units

        async def read(**_kwargs):
            if len(calls) >= 3:
                raise asyncio.CancelledError
            receipt = clock() - 0.6 if not calls else clock()
            packet = NS(
                stream=MediaStreamKind.VIDEO,
                payload=source[0][0],
                rtp_timestamp=len(calls) * 6000,
                received_at=receipt,
                capture_epoch=session.capture_epoch,
            )
            return NS(packets=(packet,), capture_epoch=session.capture_epoch, resynchronized=False)

        session.handle.read = read
        session.adapter = NS(
            feed=feed,
            configuration_generation=1,
            request_keyframe=lambda: requests.append(True),
            close=lambda: asyncio.sleep(0),
        )
        with pytest.raises(asyncio.CancelledError):
            await manager._send_video(session)
        counters = session.metrics.snapshot()["counters"]
        assert counters["stale_output_units"] == 1
        assert counters["dependency_skips"] == 2
        assert counters["sent_access_units"] == counters["sent_keyframes"] == 1
        assert requests == [True] and not session.awaiting_idr and sink.sent


@pytest.mark.asyncio
async def test_age490ms_rejects_whole_idr_before_first_packet_when_it_cannot_fit(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        seq = session.packetizer.sequence
        assert not await manager._access_unit(
            session, output(wire_idr(10000)), session.capture_epoch, received_at=clock() - 0.49
        )
        assert not sink.sent and session.packetizer.sequence == seq
        assert session.awaiting_idr and not session.retired


@pytest.mark.asyncio
async def test_partial_failure_then_fresh_idr_uses_new_indexes_and_recovers(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        clock.overshoot = 0.55
        assert not await manager._access_unit(
            session, output(wire_idr(1000)), session.capture_epoch, received_at=clock()
        )
        first_sequence = struct.unpack_from("!H", sink.sent[0][1], 2)[0]
        reserved = session.packetizer.sequence
        sink.sent.clear()
        clock.overshoot = 0
        assert await manager._access_unit(
            session, output(timestamp=6000), session.capture_epoch, received_at=clock()
        )
        current = struct.unpack_from("!H", sink.sent[0][1], 2)[0]
        assert current == reserved and current != first_sequence
        assert not session.awaiting_idr and session.recovery_attempts == 0
        assert session.recovery_since is None


@pytest.mark.asyncio
async def test_parameter_reset_cannot_restart_an_existing_recovery_budget(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, _):
        manager._need_idr(session)
        began = session.recovery_since
        session.recovery_attempts = 2
        clock.now += 2
        manager._reset_video_recovery(session)
        assert session.recovery_since == began and session.recovery_attempts == 2


@pytest.mark.asyncio
async def test_stale_actual_idr_rearms_consumed_force_without_charging_fresh_attempt(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        requests = []
        session.adapter = NS(
            request_keyframe=lambda: requests.append(True), close=lambda: asyncio.sleep(0)
        )
        session.idr_requested = True
        session.recovery_attempts = 1
        assert not await manager._access_unit(
            session, output(), session.capture_epoch, received_at=clock() - 0.501
        )
        assert requests == [True] and session.recovery_attempts == 1
        assert session.awaiting_idr and not sink.sent


@pytest.mark.asyncio
async def test_true_capture_epoch_change_preserves_view_recovery_budget(monkeypatch):
    async with controlled(monkeypatch) as (manager, controller, session, clock, _):
        entered = asyncio.Event()
        manager._need_idr(session)
        began = session.recovery_since
        session.recovery_attempts = 2
        controller.handle.buffer.clear()

        async def read(**_kwargs):
            if entered.is_set():
                raise asyncio.CancelledError
            entered.set()
            return NS(
                packets=(), capture_epoch=controller.handle.capture_epoch, resynchronized=True
            )

        controller.handle.read = read
        with pytest.raises(asyncio.CancelledError):
            await manager._send_video(session)
        assert session.capture_epoch == controller.handle.capture_epoch
        assert session.recovery_since == began and session.recovery_attempts == 2


@pytest.mark.asyncio
async def test_recovery_reaper_retires_a_sender_blocked_reading_source():
    adapter = NS(request_keyframe=lambda: None, close=lambda: asyncio.sleep(0))
    manager, controller, identity, _, receiver = await prepared(adapter_factory=lambda _: adapter)
    entered = asyncio.Event()
    ended = asyncio.Event()

    async def read(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    async def notice(*_args):
        ended.set()

    controller.handle.read = read
    manager._on_ended = notice
    try:
        await manager.start(controller.boot, controller.scope, identity, CONFIG)
        await asyncio.wait_for(entered.wait(), timeout=1)
        session = manager._sessions[identity]
        manager._need_idr(session)
        session.recovery_since -= module.RECOVERY_SECONDS + 0.01
        await manager.refresh()
        assert session.retired and session.socket.fileno() == -1
        await asyncio.wait_for(ended.wait(), timeout=1)
        assert controller.handle.closed and manager.pending_resources == 0
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_actual_idr_with_wrong_current_geometry_never_clears_recovery(monkeypatch):
    async with controlled(monkeypatch) as (manager, _, session, clock, sink):
        payload, _ = encode_source(width=640, height=360, frames=1)[0]
        with pytest.raises(RtpError, match="selection"):
            await manager._access_unit(
                session, output(payload), session.capture_epoch, received_at=clock()
            )
        assert session.awaiting_idr and not sink.sent


@pytest.mark.asyncio
@pytest.mark.parametrize("width,height,kbps", [(640, 360, 132), (1280, 720, 299)])
async def test_actual_native_motion_through_sender_pacer_and_independent_srtp_decode(
    monkeypatch, sender_motion, width, height, kbps
):
    import av

    fps, source = sender_motion
    async with controlled(monkeypatch, bitrate=kbps) as (manager, _, session, clock, sink):
        session.config = replace(
            session.config, width=width, height=height, fps=30, profile="high", mtu=1378
        )
        worker = VideoWorker(session.config)
        crypto = native_crypto(session.prepared.ssrc)
        decoder = av.CodecContext.create("h264", "r")
        decoder.thread_count = 1
        decoded_count, last_end, first = 0, 0, None
        try:
            for timestamp, payload in source:
                encoded = await worker.feed(payload, timestamp)
                assert len(encoded) <= 1
                assert worker.last_pending_frames == 1
                # Source-time rate control has one explicitly pending picture.
                # Do not borrow the newer triggering input's receipt for it.
                if not encoded:
                    assert timestamp == source[0][0]
                    continue
                native_timestamp, native_payload = encoded[0]
                if first is None:
                    first = native_payload
                receipt = 100 + native_timestamp / 90000
                # Model 250 ms source age plus 50 ms conversion allowance
                # against the triggering input, then additionally charge this
                # delayed output's actual earlier receipt. Native wall time is
                # qualified separately; pacer and crypto are production methods.
                clock.now = max(clock.now, 100 + timestamp / 90000 + 0.300)
                assert await manager._access_unit(
                    session,
                    output(native_payload, native_timestamp),
                    session.capture_epoch,
                    received_at=receipt,
                )
                sent = sink.sent[last_end:]
                last_end = len(sink.sent)
                assert sent and all(when <= receipt + 0.5 for when, _ in sent)
                clear = [crypto.unprotect(packet) for _, packet in sent]
                assert clear[-1][1] & 0x80
                picture = b"".join(b"\0\0\0\1" + nal for nal in reconstruct(clear))
                decoded = decoder.decode(av.Packet(picture))
                assert len(decoded) == 1
                assert (decoded[0].width, decoded[0].height) == (width, height)
                decoded_count += len(decoded)
            assert decoded_count == fps * 6 - 1
            assert worker.last_pending_frames == 1  # No invented live flush at STOP.
            metrics = session.metrics.snapshot()["counters"]
            assert metrics["sent_keyframes"] == 3
            assert metrics["sent_access_units"] == len(source) - 1
            assert metrics["partial_access_units"] == metrics["recovery_idr_rejected"] == 0

            # Existing source age and additional pacing backlog are additive.
            # This deliberately cannot fit, even with a small actual native IDR.
            clock.now += 1
            receipt = clock() - 0.250
            session.next_packet_at = clock() + 0.250
            sequence, count = session.packetizer.sequence, len(sink.sent)
            assert not await manager._access_unit(
                session, output(first, timestamp + 6000), session.capture_epoch, received_at=receipt
            )
            assert session.packetizer.sequence == sequence and len(sink.sent) == count
            assert session.awaiting_idr
        finally:
            await worker.close()
        assert worker.pid is None and not worker._reserved
