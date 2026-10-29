"""Bounded catchup preserves actual audio content, source clocks and fences."""

import asyncio
import socket
import struct

import pylibsrtp
import pytest
from test_homekit_audio_live_edge import drain, observe_audio_clock, setup, source

from halo_server.media import homekit_audio
from halo_server.media.homekit_audio import (
    MAX_AUDIO_AGE,
    MAX_PACING_PACKET_CREDIT,
    _AudioPacer,
)
from halo_server.media.opus import AudioConfiguration, AudioOutput


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
@pytest.mark.parametrize("packet_ms", [20, 30, 40, 60])
@pytest.mark.parametrize("bitrate", [6, 24, 64])
def test_packet_and_ip_wire_credit_are_finite_for_all_negotiated_cadences(
    family, packet_ms, bitrate
):
    pacer = _AudioPacer(AudioConfiguration(24000, packet_ms, "vbr", bitrate), family, 10)
    now, events = 10.0, []
    for index in range(100):
        # Alternate small packets and maximum admitted packets; the byte
        # allowance must constrain the latter independently of packet count.
        cost = (10 if index % 2 else 1178) + 22 + pacer.network_header
        now += pacer.delay(cost, now)
        pacer.sent(cost, now)
        events.append((now, cost))
        assert pacer._packets >= -1e-8 and pacer._bytes >= -1e-7
    assert pacer.packet_rate <= 100
    assert pacer.wire_capacity <= 3 * 1248
    for first, _ in events:
        for period in (0, 0.01, 0.1, 1):
            selected = [cost for at, cost in events if first <= at <= first + period]
            assert len(selected) <= MAX_PACING_PACKET_CREDIT + pacer.packet_rate * period + 1e-6
            assert sum(selected) <= pacer.wire_capacity + pacer.wire_rate * period + 1e-5
    # An idle interval can restore only the same finite credit.
    pacer.delay(1, now + 10000)
    assert pacer._packets == MAX_PACING_PACKET_CREDIT
    assert pacer._bytes == pacer.wire_capacity


def unit(index, size=60):
    # Framing-only sender tests preserve these exact bytes; native/independent
    # decoding of real packets is covered by the separate native audio tests.
    return AudioOutput(bytes([index + 1]) * size, index * 960, 960, "adapt")


async def queue(track, count, *, size=60, receipt=100):
    observe_audio_clock(track, source(0, receipt))
    for offset in range(0, count, 16):
        await track._audio_outputs(
            tuple(unit(index, size) for index in range(offset, min(count, offset + 16))),
            source(0, receipt),
        )


@pytest.mark.asyncio
async def test_ordinary_60ms_batch_retains_nominal_20ms_spacing(monkeypatch):
    context = setup(monkeypatch, cold=0)
    track = context.track
    try:
        await queue(track, 3)
        await drain(track)
        assert [round(at - 100, 6) for at, _packet in context.sent] == [0, 0.02, 0.04]
        assert track.diagnostics()["metrics"]["counters"]["catchup_packets"] == 0
    finally:
        await track.close()


@pytest.mark.asyncio
async def test_600ms_fresh_backlog_is_delivered_before_500ms_without_retimestamping(monkeypatch):
    context = setup(monkeypatch, cold=0)
    track = context.track
    try:
        await queue(track, 30)
        await drain(track)
        assert len(context.sent) == 30
        assert context.sent[-1][0] - 100 < 0.5
        assert all(at - 100 <= MAX_AUDIO_AGE for at, _packet in context.sent)
        timestamp = [struct.unpack("!I", packet[4:8])[0] for _at, packet in context.sent]
        assert all(
            (right - left) & 0xFFFFFFFF == 480
            for left, right in zip(timestamp, timestamp[1:], strict=False)
        )
        decoder = pylibsrtp.Session(
            pylibsrtp.Policy(
                key=track.request.key + track.request.salt,
                ssrc_type=pylibsrtp.Policy.SSRC_SPECIFIC,
                ssrc_value=track.prepared.ssrc,
            )
        )
        # Independent SRTP decoding proves the compressed bytes, rather than
        # treating the clear RTP header as evidence about encrypted content.
        assert [decoder.unprotect(packet)[12:] for _at, packet in context.sent] == [
            unit(i).payload for i in range(30)
        ]
        values = track.diagnostics()["metrics"]["counters"]
        assert values["output_stale_samples"] == values["output_budget_rejected_samples"] == 0
        assert values["output_sent_samples"] == 30 * 960
        assert values["catchup_packets"] > 0
    finally:
        await track.close()


@pytest.mark.asyncio
async def test_wire_limit_rejects_infeasible_head_without_spending_or_waiting_past_freshness(
    monkeypatch,
):
    context = setup(monkeypatch, cold=0)
    track = context.track
    track.config = AudioConfiguration(24000, 60, "vbr", 6)
    try:
        await queue(track, 4, size=1178)
        # No byte credit is available for this synthetic maximum-size head.
        track._pacer = _AudioPacer(track.config, socket.AF_INET, context.now[0])
        track._pacer._bytes = 0
        context.now[0] += 0.49
        track._pacer._at = context.now[0]
        await track._send_output(track._outputs[0])
        assert not context.sent and context.now[0] == 100.49
        assert track._pacer._packets == 3 and track._pacer._bytes == 0
        values = track.diagnostics()["metrics"]["counters"]
        assert values["output_budget_rejected_packets"] == 1
    finally:
        await track.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["privacy", "epoch", "stop", "cancel"])
async def test_catchup_wait_cannot_cross_permission_epoch_or_stop(monkeypatch, change):
    context = setup(monkeypatch, cold=0)
    track = context.track
    real_sleep = asyncio.sleep
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        await queue(track, 12)
        await track._send_output(track._outputs[0])
        sent_before = len(context.sent)

        async def wait(delay):
            entered.set()
            await release.wait()
            context.now[0] += delay
            await real_sleep(0)

        monkeypatch.setattr(homekit_audio.asyncio, "sleep", wait)
        pending = asyncio.create_task(track._send_output(track._outputs[0]))
        await asyncio.wait_for(entered.wait(), 1)
        if change == "privacy":
            context.allowed[0] = False
        elif change == "epoch":
            context.epoch[0] = 2
        elif change == "stop":
            track.fence()
        else:
            pending.cancel()
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        assert len(context.sent) == sent_before
        assert track._pacer._packets >= 2
    finally:
        release.set()
        await track.close()
