"""Delivery evidence is bounded, authenticated, and independent of media policy."""

import asyncio
import json
import struct
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from aiortc.rtp import RtcpPacket, RtcpReceiverInfo, RtcpRrPacket, RtcpSrPacket
from test_homekit_live import (
    CONFIG,
    KEY,
    SALT,
    DeferredAdapter,
    encoded_picture,
    native_crypto,
    prepared,
    reconstruct,
    wire_idr,
)

from halo_server.media import homekit_live as module
from halo_server.media.rtp import (
    MAX_RECEIVER_REPORTS,
    MAX_RECENT_SENDER_REPORTS,
    ReceiverReport,
    ReceiverReportStats,
    annexb_nalus,
    parse_feedback,
    sender_report,
)


def report(*, ssrc=9, sequence=10, lost=-2, fraction=64, jitter=900, lsr=0, dlsr=0):
    return RtcpReceiverInfo(ssrc, fraction, lost, sequence, jitter, lsr, dlsr)


def parsed_report(**kwargs):
    return parse_feedback(bytes(RtcpRrPacket(7, [report(**kwargs)])), 9).receiver_reports


def test_receiver_reports_preserve_signed_loss_and_filter_unrelated_media():
    payload = bytes(RtcpRrPacket(7, [report(), report(ssrc=10, lost=123)]))
    feedback = parse_feedback(payload, 9)
    assert feedback.reports == 1
    assert feedback.receiver_reports == (ReceiverReport(64, -2, 10, 900, 0, 0),)
    assert not feedback.pli and not feedback.nack and not feedback.bye
    # Report material is fixed-sized even when the authenticated peer repeats
    # our report block throughout a maximum legal compound packet.
    bounded = parse_feedback(bytes(RtcpRrPacket(7, [report()] * 31)) * 20, 9)
    assert bounded.reports == 620
    assert len(bounded.receiver_reports) == MAX_RECEIVER_REPORTS
    assert parse_feedback(bytes(RtcpRrPacket(7)), 9).receiver_reports == ()


def test_rr_rtt_requires_recent_sent_sr_and_receiver_delay_is_not_network_rtt():
    stats = ReceiverReportStats()
    assert stats.snapshot(10)["state"] == "unknown"
    assert stats.snapshot(10)["rtt_us"] is None
    sr = sender_report(9, 1, 2, 3, now=1234.5)
    compact = (struct.unpack_from("!Q", sr, 8)[0] >> 16) & 0xFFFFFFFF
    stats.sent(sr, 10)
    stats.observe(parsed_report(lsr=compact, dlsr=16384), 10.5)
    snapshot = stats.snapshot(10.6)
    assert snapshot["rtt_us"] == 250000  # 500 ms elapsed minus receiver's 250 ms.
    assert snapshot["jitter_us"] == 10000
    assert snapshot["packets_lost"] == -2 and snapshot["fraction_lost_256"] == 64
    assert snapshot["rtt_samples"] == 1 and snapshot["age_ms"] == 100
    assert "highest_sequence" not in snapshot and "lsr" not in snapshot
    stats.observe(parsed_report(sequence=11, lsr=compact, dlsr=65536), 10.6)
    stats.observe(parsed_report(sequence=12, lsr=compact ^ 1), 10.7)
    snapshot = stats.snapshot(10.8)
    assert snapshot["rtt_invalid"] == snapshot["rtt_unmatched"] == 1
    assert snapshot["rtt_us"] == 250000 and snapshot["rtt_age_ms"] == 300
    assert snapshot["highest_sequence_advance"] == 2
    stale = stats.snapshot(71)
    assert stale["state"] == "stale"
    assert stale["jitter_us"] is stale["fraction_lost_256"] is stale["rtt_us"] is None


def test_rr_reordering_wrap_and_history_are_bounded_without_false_freshness():
    stats = ReceiverReportStats(clock_rate=24000)
    stats.observe(parsed_report(sequence=0xFFFFFFFE, jitter=240), 1)
    stats.observe(parsed_report(sequence=3, jitter=480), 2)
    stats.observe(parsed_report(sequence=0xFFFFFFFF, jitter=999999), 3)
    snapshot = stats.snapshot(3)
    assert snapshot["highest_sequence_advance"] == 5
    assert snapshot["reordered_reports"] == 1 and snapshot["reports"] == 2
    assert snapshot["age_ms"] == 1000 and snapshot["jitter_us"] == 20000
    for index in range(1000):
        stats.sent(sender_report(9, 0, 0, 0, now=1000 + index / 100), 4 + index / 100)
    assert len(stats._sent) == MAX_RECENT_SENDER_REPORTS
    stats.observe((), 100)
    assert not stats._sent


@pytest.mark.asyncio
async def test_live_srtcp_report_is_evidence_only_after_authentication_and_matching_sr():
    adapter = DeferredAdapter(CONFIG)
    manager, controller, identity, prepared_video, receiver = await prepared(
        adapter_factory=lambda _: adapter
    )
    crypto, peer = native_crypto(prepared_video.ssrc), native_crypto(500)
    controller.handle.buffer.append_video(encoded_picture(), 90000)
    try:
        await manager.start(
            controller.boot, controller.scope, identity, replace(CONFIG, rtcp_interval=0.1)
        )
        target = ("127.0.0.1", prepared_video.port)
        own = prepared_video.ssrc
        forged = bytes(RtcpRrPacket(500, [report(ssrc=own)]))
        receiver.sendto(forged, target)
        async with asyncio.timeout(2):
            while not manager.diagnostics()["active"][0]["video"]["counters"]["feedback_rejected"]:
                await asyncio.sleep(0.005)
        assert manager.diagnostics()["active"][0]["receiver_report"]["state"] == "unknown"
        async with asyncio.timeout(2):
            while True:
                packet = await asyncio.get_running_loop().sock_recv(receiver, 2048)
                if packet[1] == 200:
                    sr = RtcpPacket.parse(crypto.unprotect_rtcp(packet))[0]
                    assert isinstance(sr, RtcpSrPacket)
                    break
                crypto.unprotect(packet)
        compact = (sr.sender_info.ntp_timestamp >> 16) & 0xFFFFFFFF
        receiver.sendto(
            peer.protect_rtcp(bytes(RtcpRrPacket(500, [report(ssrc=own, lsr=compact)]))), target
        )
        async with asyncio.timeout(2):
            while manager.diagnostics()["active"][0]["receiver_report"]["reports"] != 1:
                await asyncio.sleep(0.005)
        active = manager.diagnostics()["active"][0]
        assert active["receiver_report"]["rtt_us"] is not None
        assert active["receiver_report"]["jitter_us"] == 10000
        assert active["video"]["counters"]["source_access_units"] == 1
        assert active["video"]["counters"]["sent_access_units"] == 1
        assert active["video"]["counters"]["sent_packets"] >= 1
        assert manager.pending_resources == 1 and adapter.keyframe_requests == 0
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_partial_au_counts_packets_but_not_a_complete_frame(monkeypatch):
    manager, controller, identity, result, receiver = await prepared()
    session = manager._sessions[identity]
    session.config, session.handle = CONFIG, controller.handle
    output = SimpleNamespace(payload=wire_idr(3000), timestamp=0, mode="copy", is_keyframe=True)
    calls = 0
    protected = []

    async def send(_session, _packet, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise OSError("private address and media must never be inspected")
        protected.append(_packet)

    monkeypatch.setattr(manager, "_paced_send", send)
    try:
        with pytest.raises(OSError):
            await manager._access_unit(session, output, controller.handle.capture_epoch)
        counters = manager.diagnostics()["active"][0]["video"]["counters"]
        assert calls == 4 and counters["sent_packets"] == 3
        assert counters["sent_access_units"] == 0
        assert counters["sent_protected_bytes"] == counters["sent_payload_bytes"] + 22 * 3
        crypto = native_crypto(result.ssrc)
        clear = [crypto.unprotect(packet) for packet in protected]
        # Real matching SPS/PPS preceded the first IDR fragment. The next
        # fragment failed, so the observed unit still is genuinely incomplete.
        assert [packet[12] & 31 for packet in clear] == [7, 8, 28]
        assert clear[-1][13] & 0x80 and not clear[-1][13] & 0x40
        assert not clear[-1][1] & 0x80
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_diagnostic_failure_cannot_change_packet_bytes_or_cleanup(monkeypatch):
    manager, controller, identity, result, receiver = await prepared()
    session = manager._sessions[identity]
    session.config, session.handle = CONFIG, controller.handle

    class Broken:
        def __getattr__(self, _name):
            raise RuntimeError("private telemetry exception")

    session.metrics = Broken()
    payload = encoded_picture()
    output = SimpleNamespace(payload=payload, timestamp=0, mode="copy", is_keyframe=True)
    try:
        assert await manager._access_unit(session, output, controller.handle.capture_epoch)
        crypto, clear = native_crypto(result.ssrc), []
        async with asyncio.timeout(1):
            while True:
                encrypted = await asyncio.get_running_loop().sock_recv(receiver, 2048)
                clear.append(crypto.unprotect(encrypted))
                if clear[-1][1] & 0x80:
                    break
        assert reconstruct(clear) == annexb_nalus(payload)
        await manager.stop(controller.boot, controller.scope, identity)
        assert manager.pending_resources == 0 and controller.handle.closed
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_recent_capture_waits_for_cleanup_and_final_audio_ledger():
    manager, controller, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    entered, release = asyncio.Event(), asyncio.Event()

    class Audio:
        cleanup_failed = False
        sent_packets = sent_octets = 0
        finalized = False

        def fence(self):
            pass

        async def close(self):
            entered.set()
            await release.wait()
            self.finalized = True

        def diagnostics(self):
            return {"finalized": self.finalized}

    session.audio = Audio()
    stopping = asyncio.create_task(manager.stop(controller.boot, controller.scope, identity))
    try:
        await entered.wait()
        snapshot = manager.diagnostics()
        assert len(snapshot["active"]) == 1 and not snapshot["recent"]
        release.set()
        await stopping
        snapshot = manager.diagnostics()
        assert not snapshot["active"] and len(snapshot["recent"]) == 1
        assert snapshot["recent"][0]["audio"] == {"finalized": True}
        assert snapshot["recent"][0]["terminal"]["reason"] == "stop_request"
        snapshot["recent"][0]["video"]["counters"]["sent_packets"] = 999
        assert manager.diagnostics()["recent"][0]["video"]["counters"]["sent_packets"] == 0
        rendered = json.dumps(manager.diagnostics())
        for secret in (
            KEY.hex(),
            SALT.hex(),
            controller.boot,
            identity,
            controller.scope.profile_id,
            "127.0.0.1",
        ):
            assert secret not in rendered
        assert "ssrc" not in rendered and "lsr" not in rendered
    finally:
        release.set()
        await asyncio.gather(stopping, return_exceptions=True)
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_failed_cleanup_stays_visible_until_retry_proves_retirement():
    manager, controller, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    session.handle = controller.handle
    attempts = 0

    async def close():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("private failure must not become a diagnostic field")
        controller.handle.closed = True

    controller.handle.close = close
    try:
        with pytest.raises(OSError):
            await manager.stop(controller.boot, controller.scope, identity)
        pending = manager.diagnostics()
        assert pending["active"][0]["cleanup_pending"]
        assert pending["active"][0]["state"] == "failed" and not pending["recent"]
        await manager.stop(controller.boot, controller.scope, identity)
        retired = manager.diagnostics()
        assert not retired["active"] and len(retired["recent"]) == 1
        assert not retired["recent"][0]["cleanup_pending"] and attempts == 2
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_recent_retention_is_bounded_across_real_socket_owner_replacements():
    manager, controller, identity, _, receiver = await prepared()
    try:
        await manager.stop(controller.boot, controller.scope, identity)
        for _ in range(module.MAX_SESSIONS + 2):
            identity = str(uuid4())
            await manager.prepare(
                controller.boot,
                controller.scope,
                identity,
                source_address="127.0.0.1",
                target_address="127.0.0.1",
                address_version="ipv4",
                target_port=receiver.getsockname()[1],
                key=KEY,
                salt=SALT,
            )
            await manager.stop(controller.boot, controller.scope, identity)
        snapshot = manager.diagnostics()
        assert not snapshot["active"] and len(snapshot["recent"]) == module.MAX_SESSIONS
        assert all(item["state"] == "stopped" for item in snapshot["recent"])
    finally:
        receiver.close()
        await manager.close()


@pytest.mark.asyncio
async def test_reaper_records_distinct_loop_lag_without_an_extra_task(monkeypatch):
    manager, _, identity, _, receiver = await prepared()
    session = manager._sessions[identity]
    session.state = "active"
    manager._reaper.cancel()
    await asyncio.gather(manager._reaper, return_exceptions=True)
    now = [10.0]
    manager._clock = lambda: now[0]

    async def sleep(seconds):
        assert seconds == 0.25
        now[0] += 0.375

    async def refresh():
        session.retired = True
        manager._sessions.clear()

    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    monkeypatch.setattr(manager, "refresh", refresh)
    try:
        await manager._expire()
        timing = session.metrics.snapshot()["timings"]["loop_lag"]
        assert timing["count"] == 1 and timing["max_us"] == 125000
    finally:
        # This synthetic loop test removed the owner only to terminate its loop.
        manager._sessions[identity] = session
        receiver.close()
        await manager.close()
