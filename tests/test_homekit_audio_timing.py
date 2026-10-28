"""Bounded audio metadata distinguishes source gaps without retaining media."""

import asyncio
import json
import logging
import socket
import struct
from types import SimpleNamespace

import pytest

from halo_server.media import homekit_audio
from halo_server.media.homekit_audio import AudioEndpointRequest, AudioTrack, _AudioTiming
from halo_server.media.opus import AudioConfiguration, AudioOutput


def packet(timestamp, *, received=100.0, payload=b"\x5c\x01"):
    # One stereo SILK/WB 60ms frame, framing only (never decoded).
    return SimpleNamespace(
        payload=payload, rtp_timestamp=timestamp, received_at=received, capture_epoch=1
    )


def records(caplog):
    return [
        (record.args[0], json.loads(record.args[1]))
        for record in caplog.records
        if record.msg == "HomeKit audio timing phase=%s metrics=%s"
    ]


def test_source_duration_step_mismatch_and_bounded_private_summaries(caplog):
    caplog.set_level(logging.INFO, logger=homekit_audio.__name__)
    timing = _AudioTiming(AudioConfiguration(24000, 20, "cbr", 24), 100)
    # Large source timestamps never leave the diagnostic object. Real 60ms
    # frames with 20ms source timestamp steps must be visible as mismatches.
    for index in range(3):
        timing.source(packet(2_000_000_000 + index * 960), 100.1)
    timing.source(packet(2_000_001_920, payload=b""), 100.2)
    for now in (104.99, 105, 105.01, 115, 130, 1000, 2000):
        timing.tick(now)
    timing.finish(2000)
    timing.finish(3000)
    timing.tick(3000)
    result = records(caplog)
    assert [phase for phase, _value in result] == ["5s", "15s", "30s", "terminal"]
    value = result[-1][1]
    assert value["input_packets"] == 4 and value["invalid_toc"] == 1
    assert value["duration_2880"] == value["frame_2880"] == 3
    assert value["channel_2"] == value["bandwidth_16000"] == 3
    assert value["input_step_mismatch"] == 2 and value["input_gap_max"] == 1920
    assert value["input_step_max"] == 960
    assert len(caplog.text) < 12_000
    assert "2000000000" not in caplog.text
    assert all(type(item) in {int, str} for item in value.values())
    assert timing.previous_input == (2_000_001_920, 2880)
    assert not any(isinstance(item, bytes) for item in vars(timing).values())


def test_counters_saturate_and_wrap_safe_wire_delta():
    timing = _AudioTiming(AudioConfiguration(24000, 20, "vbr", 24), 0)
    timing.count("input_bytes", 10**30)
    timing.count("input_bytes")
    timing.maximum("feed_latency_ms_max", 10**30)
    timing.sent(0xFFFFFF00, 480, 10, 1, 0.99)
    timing.sent((0xFFFFFF00 + 480) & 0xFFFFFFFF, 480, 10, 1.02, 1.01)
    assert timing.values["input_bytes"] == 2**31 - 1
    assert timing.values["feed_latency_ms_max"] == 2**31 - 1
    assert timing.values["wire_step_max"] == 480
    assert timing.values["wire_step_mismatch"] == 0
    assert timing.values["sent_packets"] == 2


def make_track(monkeypatch, now):
    class Socket:
        def setblocking(self, _value):
            pass

        def setsockopt(self, *_values):
            pass

        def bind(self, _value):
            pass

        def connect(self, _value):
            pass

        def getsockname(self):
            return "127.0.0.1", 50000

        def fileno(self):
            return -1

        def close(self):
            pass

    monkeypatch.setattr(homekit_audio.socket, "socket", lambda *_args: Socket())
    return AudioTrack(
        AudioEndpointRequest(50001, b"private-key-text", b"private-salt!!"),
        bind=("127.0.0.1", 0),
        endpoint=("127.0.0.1", 50001),
        family=socket.AF_INET,
        excluded_ssrc=1,
        check=lambda _permission: None,
        epoch=lambda: 1,
        controller=None,
        boot="private-boot-identity",
        scope="private-scope-identity",
        failure=lambda _code: None,
        presentation=lambda _kind, _timestamp, receipt: receipt,
        touch_feedback=lambda _value: None,
        last_feedback=lambda: now[0],
        clock=lambda: now[0],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("broken_logger", [False, True])
async def test_real_send_loop_outcomes_unchanged_with_safe_or_failing_trace(
    monkeypatch, caplog, broken_logger
):
    caplog.set_level(logging.INFO, logger=homekit_audio.__name__)
    now = [100.0]
    track = make_track(monkeypatch, now)
    track.config = AudioConfiguration(24000, 20, "cbr", 24)
    track._timing = _AudioTiming(track.config, now[0])
    calls, sent = [], []

    class Adapter:
        async def feed(self, payload, timestamp, *, capture_epoch):
            calls.append((payload, timestamp, capture_epoch))
            now[0] += 0.003
            return tuple(
                AudioOutput(b"\xd8\x01", timestamp + offset, 960, "adapt")
                for offset in (0, 960, 1920)
            )

        async def close(self):
            pass

    class Handle:
        async def read_audio(self, *, timeout):
            if calls:
                raise asyncio.CancelledError
            return SimpleNamespace(
                capture_epoch=1,
                packets=(
                    packet(2_000_000_000),
                    packet(2_000_000_960),
                    packet(2_000_001_920, received=90),
                ),
            )

    async def send(data, **_options):
        sent.append(bytes(data))
        return True

    async def sleep(delay):
        now[0] += delay

    def broken(*_args, **_kwargs):
        raise asyncio.CancelledError("private-error-text")

    track._adapter, track.handle = Adapter(), Handle()
    monkeypatch.setattr(track, "_send", send)
    monkeypatch.setattr(homekit_audio.asyncio, "sleep", sleep)
    if broken_logger:
        monkeypatch.setattr(homekit_audio._LOGGER, "info", broken)
    try:
        with pytest.raises(asyncio.CancelledError):
            await track._send_audio()
        assert len(calls) == 2 and len(sent) == track.sent_packets == 6
        timestamps = [struct.unpack("!I", data[4:8])[0] for data in sent]
        assert (timestamps[1] - timestamps[0]) & 0xFFFFFFFF == 480
        assert track.failure is None
        assert track._timing.values["stale_input"] == 1
        assert track._timing.values["feed_calls"] == 2
        assert track._timing.values["output_packets"] == 6
        assert track._timing.values["output_pts_mismatch"] == 1
        assert track._timing.values["input_gap_max"] == 1920
        assert track._timing.values["mode_adapt"] == 6
        assert track._timing.values["presentation_age_ms_max"] >= 80
    finally:
        await track.close()
    assert track._retired and track._timing.finished
    if not broken_logger:
        assert len(records(caplog)) == 1
    for secret in ("private-key", "private-salt", "private-boot", "private-scope", "private-error"):
        assert secret not in caplog.text
