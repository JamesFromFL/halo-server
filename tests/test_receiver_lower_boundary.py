"""Synthetic real aiortc/aioice brackets; no socket receive or account access."""

import asyncio
import json
from types import SimpleNamespace as NS

import aioice
import pytest
from aioice.ice import StunProtocol
from aiortc import RTCPeerConnection
from aiortc.rtp import (
    RTCP_PSFB_APP,
    RtcpPsfbPacket,
    RtcpRrPacket,
    RtpPacket,
    pack_remb_fci,
)
from pylibsrtp import Policy, Session

from halo_server.media import receiver_metrics as module
from halo_server.media.receiver_metrics import MAX_QUEUE_TIMES, ReceiverPipeline
from halo_server.media.session import _safe_pipeline


class Transport(asyncio.DatagramTransport):
    def sendto(self, *_args):
        pass


def fixture():
    pc = RTCPeerConnection()
    audio = pc.addTransceiver("audio").receiver
    video = pc.addTransceiver("video").receiver
    video._RTCRtpReceiver__transport = audio.transport
    dtls = audio.transport
    connection = dtls.transport._connection
    protocol = StunProtocol(connection)
    protocol.local_candidate = NS(component=1)
    protocol.transport = Transport()
    connection._protocols.append(protocol)
    connection._nominated[1] = NS(protocol=protocol)
    now = [100.0]
    pipeline = ReceiverPipeline(audio, video, clock=lambda: now[0])
    key = b"k" * 30
    outbound = Session(Policy(key=key, ssrc_type=Policy.SSRC_ANY_OUTBOUND))
    dtls._rx_srtp = Session(Policy(key=key, ssrc_type=Policy.SSRC_ANY_INBOUND))
    dtls.encrypted = True
    return pc, audio, video, dtls, connection, protocol, now, pipeline, outbound


async def close(pc, pipeline):
    pipeline.fence()
    # Fake protocol objects have no real native socket/transport to retire.
    for transceiver in pc.getTransceivers():
        transceiver.receiver.transport.transport._connection._protocols.clear()
    await pc.close()


def protected(outbound, sequence=1):
    packet = RtpPacket(payload_type=111, sequence_number=sequence, timestamp=2880 * sequence)
    packet.ssrc, packet.payload = 123, b"\xf8\xff\xfe"
    return outbound.protect(packet.serialize())


@pytest.mark.asyncio
async def test_real_udp_queue_dequeue_unprotect_and_postdecrypt_boundaries_are_distinct():
    pc, _, _, dtls, connection, protocol, now, pipeline, outbound = fixture()
    try:
        pipeline.install_lower_boundaries()
        assert pipeline.snapshot()["lower_boundary"]["installed"]
        wire = protected(outbound)
        protocol.datagram_received(wire, ("127.0.0.1", 12345))
        queued = pipeline.snapshot()["lower_boundary"]
        assert queued["connections"] == queued["protocols"] == 1
        assert queued["metrics"]["counters"]["udp_rtp"] == 1
        assert queued["metrics"]["counters"]["ice_enqueued"] == 1
        assert queued["metrics"]["counters"]["dtls_rtp"] == 0
        now[0] += 0.65
        await dtls._recv_next()
        sampled = pipeline.snapshot()["lower_boundary"]
        counters = sampled["metrics"]["counters"]
        assert counters["ice_dequeued"] == counters["ice_rtp"] == counters["dtls_rtp"] == 1
        assert sampled["metrics"]["timings"]["ice_queue_wait"]["max_us"] == 650000
        assert connection._queue.qsize() == 0
        assert sampled["udp_drops"] is None
        assert sampled["bins"][0]["udp_rtp"] == 1
        assert sampled["bins"][6]["dtls_rtp"] == 1
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_stun_callback_is_not_falsely_counted_as_queued_media():
    pc, _, _, _, connection, protocol, _, pipeline, _ = fixture()
    try:
        pipeline.install_lower_boundaries()
        packet = aioice.stun.Message(aioice.stun.Method.BINDING, aioice.stun.Class.REQUEST)
        protocol.datagram_received(bytes(packet), ("127.0.0.1", 12345))
        sampled = pipeline.snapshot()["lower_boundary"]
        counters = sampled["metrics"]["counters"]
        assert counters["udp_stun"] == 1
        assert counters["udp_rtp"] == counters["ice_enqueued"] == connection._queue.qsize() == 0
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_failed_unprotect_is_distinct_from_missing_udp_data():
    pc, _, _, dtls, _, protocol, _, pipeline, outbound = fixture()
    try:
        pipeline.install_lower_boundaries()
        bad = protected(outbound)[:-1] + b"x"
        protocol.datagram_received(bad, ("127.0.0.1", 12345))
        await dtls._recv_next()
        counters = pipeline.snapshot()["lower_boundary"]["metrics"]["counters"]
        assert counters["udp_rtp"] == counters["ice_rtp"] == 1
        assert counters["dtls_rtp"] == 0
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_control_classification_and_original_return_are_unchanged():
    pc, _, _, dtls, _, protocol, _, pipeline, _ = fixture()
    seen = []

    async def write_ssl():
        seen.append(True)

    dtls._ssl = NS(bio_write=lambda _data: None, recv=lambda _size: b"")
    dtls._write_ssl = write_ssl
    try:
        pipeline.install_lower_boundaries()
        protocol.datagram_received(b"\x16synthetic-control", ("127.0.0.1", 12345))
        assert await dtls._recv_next() is None
        counters = pipeline.snapshot()["lower_boundary"]["metrics"]["counters"]
        assert counters["udp_dtls"] == counters["ice_dtls"] == counters["dtls_control"] == 1
        assert not counters["udp_rtp"] and seen == [True]
    finally:
        dtls._ssl = None
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_bounded_queue_timing_overflow_is_unknown_not_false_zero_or_wrong_packet():
    pc, _, _, dtls, connection, _, now, pipeline, _ = fixture()
    try:
        pipeline.install_lower_boundaries()
        owner = pipeline._lower
        state = owner._states[0]
        for _index in range(MAX_QUEUE_TIMES + 20):
            connection.data_received(b"\x80plain", 1)
        assert len(state.times) <= MAX_QUEUE_TIMES
        assert state.unknown == MAX_QUEUE_TIMES + 1
        for _index in range(MAX_QUEUE_TIMES + 20):
            now[0] += 0.001
            await dtls.transport._recv()
        counters = pipeline.snapshot()["lower_boundary"]["metrics"]["counters"]
        assert counters["metadata_overflows"] == 1
        assert counters["queue_wait_unknown"] == MAX_QUEUE_TIMES + 1
        assert not state.times and not state.unknown
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_already_queued_prefix_is_unknown_and_cannot_consume_new_packet_timing():
    pc, _, _, dtls, connection, _, now, pipeline, _ = fixture()
    try:
        connection.data_received(b"old", 1)
        pipeline.install_lower_boundaries()
        now[0] += 0.65
        connection.data_received(b"new", 1)
        assert await dtls.transport._recv() == b"old"
        metrics = pipeline.snapshot()["lower_boundary"]["metrics"]
        assert metrics["counters"]["queue_wait_unknown"] == 1
        assert metrics["timings"]["ice_queue_wait"]["count"] == 0
        now[0] += 0.2
        assert await dtls.transport._recv() == b"new"
        metrics = pipeline.snapshot()["lower_boundary"]["metrics"]
        assert metrics["timings"]["ice_queue_wait"]["count"] == 1
        assert metrics["timings"]["ice_queue_wait"]["max_us"] == 200000
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ValueError("not exported"), asyncio.CancelledError()])
async def test_all_telemetry_failures_preserve_original_delivery_and_error(failure):
    pc, _, _, dtls, connection, protocol, _, pipeline, outbound = fixture()
    try:
        pipeline.install_lower_boundaries()
        owner = pipeline._lower

        def broken(*_args):
            raise failure

        owner._record = owner._observe = owner._enqueue = owner._dequeue = broken
        protocol.datagram_received(protected(outbound), ("127.0.0.1", 12345))
        assert connection._queue.qsize() == 1
        assert await dtls._recv_next() is None
        assert connection._queue.qsize() == 0
        # An original ICE failure must remain the same failure, never be hidden.
        connection._nominated.clear()
        with pytest.raises(ConnectionError):
            await dtls._recv_next()
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_rtcp_attempt_types_and_remb_numeric_bins_preserve_original_callback():
    pc, audio, _, dtls, _, _, now, pipeline, _ = fixture()
    seen = []

    async def send(data):
        seen.append(data)

    dtls._send_rtp = send
    try:
        pipeline.install_lower_boundaries()
        await audio._send_rtcp(
            RtcpPsfbPacket(
                fmt=RTCP_PSFB_APP, ssrc=77, media_ssrc=0, fci=pack_remb_fci(3000000, [123])
            )
        )
        now[0] += 0.05
        await audio._send_rtcp(
            RtcpPsfbPacket(
                fmt=RTCP_PSFB_APP, ssrc=77, media_ssrc=0, fci=pack_remb_fci(150000, [123])
            )
        )
        await audio._send_rtcp(RtcpRrPacket(ssrc=77, reports=[]))
        snapshot = pipeline.snapshot()["lower_boundary"]
        assert snapshot["metrics"]["counters"]["rtcp_remb_attempt"] == 2
        assert snapshot["metrics"]["counters"]["rtcp_rr_attempt"] == 1
        assert snapshot["bins"][0]["remb_bitrate_min"] == 150000
        assert snapshot["bins"][0]["remb_bitrate_max"] == 3000000
        assert len(seen) == 3
        serialized = json.dumps(snapshot)
        assert all(
            word not in serialized for word in ("ssrc", "address", "payload", "12345", "127.0.0.1")
        )
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_fence_restores_exact_methods_and_stale_reference_cannot_emit_metadata():
    pc, audio, _, dtls, connection, protocol, _, pipeline, _ = fixture()
    original_recv = dtls.transport._recv
    try:
        pipeline.install_lower_boundaries()
        old_udp = protocol.datagram_received
        owner = pipeline._lower
        pipeline.fence()
        assert dtls.transport._recv == original_recv
        assert all(
            name not in dtls.__dict__
            for name in ("_recv_next", "_handle_rtp_data", "_handle_rtcp_data")
        )
        assert "data_received" not in connection.__dict__
        assert "datagram_received" not in protocol.__dict__
        assert "_send_rtcp" not in audio.__dict__
        old_udp(b"\x80plain", ("127.0.0.1", 12345))
        assert connection._queue.qsize() == 1
        assert owner.snapshot() is None and not owner._hooks and not owner._states
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_partial_install_failure_rolls_back_every_hook_without_failing_media(monkeypatch):
    pc, _, _, dtls, connection, protocol, _, pipeline, _ = fixture()
    original = module.LowerReceiverBoundary._attach
    calls = []

    def injected(self, *args):
        calls.append(True)
        if len(calls) == 4:
            raise RuntimeError("private initialization failure")
        return original(self, *args)

    monkeypatch.setattr(module.LowerReceiverBoundary, "_attach", injected)
    try:
        pipeline.install_lower_boundaries()
        assert pipeline.snapshot()["lower_boundary"]["installed"] is False
        assert "data_received" not in connection.__dict__
        assert "_recv_next" not in dtls.__dict__
        assert "datagram_received" not in protocol.__dict__
        protocol.datagram_received(b"\x80plain", ("127.0.0.1", 12345))
        assert connection._queue.qsize() == 1
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_optional_status_lower_schema_is_whitelisted_and_copied():
    pc, _, _, _, _, protocol, _, pipeline, _ = fixture()
    try:
        pipeline.install_lower_boundaries()
        protocol.datagram_received(b"\x80plain", ("127.0.0.1", 12345))
        raw = pipeline.snapshot()
        raw["lower_boundary"]["secret"] = "never expose"
        raw["lower_boundary"]["bins"][0]["payload"] = "never expose"
        safe = _safe_pipeline(raw)
        assert safe["lower_boundary"]["installed"]
        assert "never expose" not in json.dumps(safe)
        safe["lower_boundary"]["bins"][0]["udp_rtp"] = 999
        assert pipeline.snapshot()["lower_boundary"]["bins"][0]["udp_rtp"] == 1
        raw["lower_boundary"]["metrics"]["counters"]["udp_rtp"] = -1
        assert "lower_boundary" not in _safe_pipeline(raw)
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_real_loopback_udp_callback_reaches_same_owned_decrypt_pipeline():
    import socket

    pc, _, _, dtls, connection, protocol, _, pipeline, outbound = fixture()
    transport = None
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: protocol, local_addr=("127.0.0.1", 0)
        )
        pipeline.install_lower_boundaries()
        sender.sendto(protected(outbound), transport.get_extra_info("sockname"))
        async with asyncio.timeout(1):
            while not connection._queue.qsize():
                await asyncio.sleep(0.001)
        await dtls._recv_next()
        counts = pipeline.snapshot()["lower_boundary"]["metrics"]["counters"]
        assert counts["udp_rtp"] == counts["ice_enqueued"] == counts["dtls_rtp"] == 1
    finally:
        sender.close()
        pipeline.fence()
        if transport is not None:
            transport.close()
            await asyncio.sleep(0)
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_unsupported_or_too_many_protocols_leave_all_media_methods_unchanged(monkeypatch):
    pc, _, _, dtls, connection, protocol, _, pipeline, _ = fixture()
    original_recv = dtls.transport._recv
    try:
        monkeypatch.setattr(aioice, "__version__", "unsupported")
        pipeline.install_lower_boundaries()
        assert pipeline.snapshot()["lower_boundary"]["installed"] is False
        assert dtls.transport._recv == original_recv
        assert "data_received" not in connection.__dict__
        protocol.datagram_received(b"\x80plain", ("127.0.0.1", 12345))
        assert connection._queue.qsize() == 1
        pipeline.fence()
        pipeline.install_lower_boundaries()
        assert pipeline._lower is None  # A retired owner cannot reinstall hooks.
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_bounded_flood_rolls_bins_without_retaining_payloads_or_spawning_tasks():
    import gc
    import weakref

    class Payload:
        pass

    pc, _, _, dtls, connection, _, now, pipeline, _ = fixture()
    try:
        pipeline.install_lower_boundaries()
        owner = pipeline._lower
        tasks = len(asyncio.all_tasks())
        value = Payload()
        reference = weakref.ref(value)
        connection.data_received(value, 1)
        returned = await dtls.transport._recv()
        assert returned is value
        value = returned = None
        gc.collect()
        assert reference() is None
        for index in range(3000):
            now[0] = 100 + index * 0.01
            connection.data_received(b"\x80plain", 1)
            await dtls.transport._recv()
        sampled = pipeline.snapshot()["lower_boundary"]
        assert len(sampled["bins"]) == 64
        assert len(owner._previous) <= len(module.LOWER_FIELDS)
        assert len(owner._states[0].times) == 0
        assert len(asyncio.all_tasks()) == tasks
        assert sampled["metrics"]["counters"]["ice_dequeued"] == 3001
    finally:
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_install_and_retirement_serialize_without_late_hook_reactivation(monkeypatch):
    import threading

    pc, _, _, dtls, connection, protocol, _, pipeline, _ = fixture()
    entered, release = threading.Event(), threading.Event()
    original = module.LowerReceiverBoundary._attach
    calls = []

    def held(self, *args):
        result = original(self, *args)
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(module.LowerReceiverBoundary, "_attach", held)
    install = asyncio.create_task(asyncio.to_thread(pipeline.install_lower_boundaries))
    retire = None
    try:
        async with asyncio.timeout(1):
            while not entered.is_set():
                await asyncio.sleep(0.001)
        retire = asyncio.create_task(asyncio.to_thread(pipeline.fence))
        await asyncio.sleep(0.01)
        assert not retire.done()
        release.set()
        await asyncio.gather(install, retire)
        assert pipeline.snapshot() is None and pipeline._lower is None
        assert "data_received" not in connection.__dict__
        assert "_recv_next" not in dtls.__dict__
        assert "datagram_received" not in protocol.__dict__
    finally:
        release.set()
        await asyncio.gather(install, *(tuple([retire]) if retire else ()))
        await close(pc, pipeline)


@pytest.mark.asyncio
async def test_cancelled_empty_receive_preserves_cancellation_and_retirement():
    pc, _, _, dtls, _, _, _, pipeline, _ = fixture()
    try:
        pipeline.install_lower_boundaries()
        pending = asyncio.create_task(dtls._recv_next())
        await asyncio.sleep(0)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not pipeline.snapshot()["lower_boundary"]["metrics"]["counters"]["ice_dequeued"]
    finally:
        await close(pc, pipeline)
