"""R1 duration accounting observes the existing live-audio decisions."""

import asyncio
import json
import struct
from types import SimpleNamespace

import pytest
from aiortc.rtp import RtcpReceiverInfo, RtcpRrPacket
from test_homekit_audio_live_edge import setup, source

from halo_server.media.rtp import ReceiverReportStats, sender_report
from halo_server.media.srtp import SecureVideo


def batch(packets):
    return SimpleNamespace(capture_epoch=1, packets=tuple(packets), resynchronized=False)


def counters(context):
    return context.track.diagnostics()["metrics"]["counters"]


@pytest.mark.asyncio
async def test_initial_live_tail_is_separate_from_continuous_submitted_and_sent_duration(
    monkeypatch,
):
    context = setup(monkeypatch)
    try:
        await context.track._audio_batch(batch(source(index, 100) for index in (0, 1, 2)))
        values = counters(context)
        assert values["source_read_packets"] == 3
        assert values["source_read_samples"] == 8640
        assert values["source_initial_skipped_samples"] == 5760
        assert values["source_recovery_skipped_samples"] == 0
        assert values["source_submitted_samples"] == 2880
        assert values["output_generated_samples"] == values["output_sent_samples"] == 2880
        assert context.track.sent_packets == len(context.sent) == 3
        assert context.track.diagnostics()["native_pending_samples"] is None
        assert context.track.diagnostics()["sample_clock_rate"] == 48000
    finally:
        await context.track.close()


@pytest.mark.asyncio
async def test_whole_fresh_read_discarded_by_expired_prefix_is_fully_counted(monkeypatch):
    context = setup(monkeypatch)
    context.track._capture_epoch = 1
    context.track._live_edge_pending = False
    context.track._input_spans.append((0, 2880, 99.0))
    try:
        await context.track._audio_batch(batch(source(index, 100) for index in (1, 2, 3)))
        values = counters(context)
        # This is the existing defect's measurement, not its R2 correction.
        assert len(context.factories) == 2 and context.calls == [] and context.sent == []
        assert values["source_read_samples"] == values["source_prefix_discarded_samples"] == 8640
        assert values["source_unprocessed_samples"] == values["source_submitted_samples"] == 0
        assert values["adapter_recreates"] == 1
        timings = context.track.diagnostics()["metrics"]["timings"]
        assert timings["retirement"]["count"] == 1
    finally:
        await context.track.close()


@pytest.mark.asyncio
async def test_stale_first_output_counts_native_unvisited_tail_and_unfed_input_remainder(
    monkeypatch,
):
    context = setup(monkeypatch, cold=0.6)
    context.track._capture_epoch = 1
    context.track._live_edge_pending = False
    try:
        await context.track._audio_batch(batch(source(index, 100) for index in (0, 1)))
        values = counters(context)
        assert values["source_read_samples"] == 5760
        assert values["source_submitted_samples"] == values["source_unprocessed_samples"] == 2880
        assert values["output_generated_samples"] == 2880
        assert values["output_iterated_samples"] == values["output_stale_samples"] == 960
        assert values["output_unvisited_samples"] == 1920
        assert values["output_unfinished_samples"] == values["output_sent_samples"] == 0
        assert context.sent == [] and len(context.calls) == 1
        timings = context.track.diagnostics()["metrics"]["timings"]
        assert timings["feed"]["sum_us"] == 600000
    finally:
        await context.track.close()


@pytest.mark.asyncio
async def test_cancelled_read_wait_and_authority_calls_are_observed_without_changing_cancellation(
    monkeypatch,
):
    context = setup(monkeypatch)

    class Handle:
        async def read_audio(self, *, timeout):
            context.now[0] += timeout
            raise asyncio.CancelledError

    context.track.handle = Handle()
    try:
        with pytest.raises(asyncio.CancelledError):
            await context.track._send_audio()
        metrics = context.track.diagnostics()["metrics"]
        assert metrics["timings"]["read_wait"]["count"] == 1
        assert metrics["timings"]["read_wait"]["sum_us"] == 250000
        assert metrics["timings"]["authority"]["count"] >= 2
        assert metrics["counters"]["read_batches"] == 0
        assert context.track.failure is None
    finally:
        await context.track.close()


@pytest.mark.asyncio
async def test_metric_observer_failure_cannot_change_packet_delivery_or_retirement(monkeypatch):
    context = setup(monkeypatch)

    def broken(*_args):
        raise asyncio.CancelledError("private-error-text")

    monkeypatch.setattr(context.track._metrics, "observe", broken)
    try:
        await context.track._audio_batch(batch((source(0, 100),)))
        assert len(context.sent) == context.track.sent_packets == 3
        assert counters(context)["output_sent_packets"] == 3
        assert context.track.failure is None
    finally:
        await context.track.close()
    assert context.track._retired


@pytest.mark.asyncio
async def test_repeated_status_remains_fixed_size_private_and_labels_malformed_duration_unknown(
    monkeypatch,
):
    context = setup(monkeypatch)
    original_schema = set(counters(context))
    try:
        for index in range(1000):
            ledger = context.track._source_ledger((source(index, 100),))
            ledger.remaining()
        malformed = source(1001, 99)
        malformed.payload = b""
        await context.track._audio_batch(batch((malformed,)))
        result = context.track.diagnostics()
        values = result["metrics"]["counters"]
        assert set(values) == original_schema
        assert values["source_read_packets"] == 1001
        assert values["source_read_samples"] == 1000 * 2880
        assert values["source_invalid_packets"] == 1
        assert values["source_stale_discarded_packets"] == 1
        assert values["source_stale_discarded_samples"] == 0
        assert len(json.dumps(result)) < 7000
        assert context.track._input_spans == context.track._input_spans.__class__()
        serialized = json.dumps(result)
        for secret in ("private-key", "private-salt", "private-boot", "private-scope", "payload"):
            assert secret not in serialized
    finally:
        await context.track.close()


@pytest.mark.asyncio
async def test_authenticated_audio_receiver_report_uses_selected_clock_and_own_sender_report(
    monkeypatch,
):
    context = setup(monkeypatch)
    track = context.track
    track._receiver_reports = ReceiverReportStats(clock_rate=track.config.sample_rate)
    report = sender_report(track.prepared.ssrc, 123, 10, 600)
    compact = (struct.unpack_from("!Q", report, 8)[0] >> 16) & 0xFFFFFFFF
    track._receiver_metric("sent", report, context.now[0])
    peer = SecureVideo(track.request.key, track.request.salt, 99)

    def feedback(target):
        clear = bytes(
            RtcpRrPacket(
                ssrc=99,
                reports=[
                    RtcpReceiverInfo(
                        ssrc=target,
                        fraction_lost=64,
                        packets_lost=3,
                        highest_sequence=1000,
                        jitter=240,
                        lsr=compact,
                        dlsr=32768,
                    )
                ],
            )
        )
        return peer.protect_rtcp(clear)

    messages = [feedback(track.prepared.ssrc), feedback((track.prepared.ssrc + 1) & 0xFFFFFFFF)]

    class Loop:
        async def sock_recv(self, _socket, _size):
            if messages:
                return messages.pop(0)
            raise asyncio.CancelledError

    context.now[0] += 0.75
    try:
        with monkeypatch.context() as patch:
            patch.setattr(asyncio, "get_running_loop", lambda: Loop())
            with pytest.raises(asyncio.CancelledError):
                await track._receive()
        result = track.diagnostics()["receiver_reports"]
        assert result["reports"] == 1 and result["fraction_lost_256"] == 64
        assert result["clock_rate"] == 24000 and result["jitter_us"] == 10000
        assert result["rtt_us"] == 250000 and result["rtt_samples"] == 1
        assert result["highest_sequence_advance"] == 0
        assert track.failure is None and track._last_feedback == context.now[0]
        serialized = json.dumps(result)
        for private_field in ("ssrc", "lsr", "dlsr", 'highest_sequence"', "123"):
            assert private_field not in serialized
    finally:
        peer.close()
        await track.close()
