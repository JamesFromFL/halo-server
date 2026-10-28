"""Fresh live starts avoid replay-driven conversion retirement cascades."""

import asyncio
import struct
from types import SimpleNamespace

import pytest
from test_homekit_audio_timing import make_track

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media import homekit_audio
from halo_server.media.homekit_audio import _AudioTiming
from halo_server.media.opus import AudioConfiguration, AudioOutput


def source(index, received, *, epoch=1):
    return SimpleNamespace(
        payload=b"\x58\x01",  # Valid single-frame mono WB/SILK60ms framing.
        rtp_timestamp=index * 2880,
        received_at=received,
        capture_epoch=epoch,
    )


def setup(monkeypatch, *, cold=0.122):
    now, epoch, allowed = [100.0], [1], [True]
    track = make_track(monkeypatch, now)
    track.config = AudioConfiguration(24000, 20, "cbr", 24)
    track._timing = _AudioTiming(track.config, now[0])
    calls, factories, sent = [], [], []

    def check(_permission):
        if not allowed[0]:
            raise HomeKitOperationError("policy_denied")

    class Adapter:
        def __init__(self, _config):
            self.cold, self.closed = True, False
            factories.append(self)

        async def feed(self, _payload, timestamp, *, capture_epoch):
            now[0] += cold if self.cold else 0.001
            self.cold = False
            calls.append((timestamp, capture_epoch))
            return tuple(
                AudioOutput(b"\xd8\x01", timestamp - 120 + step, 960, "adapt")
                for step in (0, 960, 1920)
            )

        async def close(self):
            self.closed = True

    async def send(data, **options):
        check(options["permission"])
        if options["epoch"] != epoch[0] or now[0] > options["fresh_until"]:
            return False
        sent.append((now[0], bytes(data)))
        return True

    async def sleep(delay):
        now[0] += delay

    track._check, track._epoch = check, lambda: epoch[0]
    track._adapter_factory = Adapter
    track._adapter = Adapter(track.config)
    monkeypatch.setattr(track, "_send", send)
    monkeypatch.setattr(homekit_audio.asyncio, "sleep", sleep)
    return SimpleNamespace(
        now=now,
        epoch=epoch,
        allowed=allowed,
        track=track,
        calls=calls,
        factories=factories,
        sent=sent,
    )


async def until_end(track):
    try:
        with pytest.raises(asyncio.CancelledError):
            await track._send_audio()
    finally:
        await track.close()


@pytest.mark.asyncio
async def test_buffered_live_start_escapes_cold_worker_stale_reset_cascade(monkeypatch):
    context = setup(monkeypatch)
    cursor = -1

    class Handle:
        async def read_audio(self, *, timeout):
            nonlocal cursor
            if context.now[0] >= 104:
                raise asyncio.CancelledError
            latest = int(round((context.now[0] - 98.02) / 0.06, 8))
            if latest <= cursor:
                context.now[0] = 98.02 + (cursor + 1) * 0.06
                latest = cursor + 1
            packets = tuple(source(i, 98.02 + i * 0.06) for i in range(cursor + 1, latest + 1))
            cursor = latest
            return SimpleNamespace(capture_epoch=1, resynchronized=False, packets=packets)

    context.track.handle = Handle()
    await until_end(context.track)
    values = context.track._timing.values
    assert len(context.factories) == 1
    assert values["adapter_recreates"] == values["stale_input"] == values["stale_output"] == 0
    assert values["live_edge_skipped"] == 33
    assert context.calls[0][0] == 33 * 2880
    assert context.track.sent_packets >= 190
    assert (
        max(b[0] - a[0] for a, b in zip(context.sent, context.sent[1:], strict=False)) <= 0.020001
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["continuous", "source_gap", "resynchronized", "epoch"])
async def test_continuous_batches_ordered_and_explicit_recovery_starts_latest(
    monkeypatch, recovery
):
    context = setup(monkeypatch, cold=0.01)
    reads = 0

    class Handle:
        async def read_audio(self, *, timeout):
            nonlocal reads
            reads += 1
            if reads == 1:
                packets = (source(0, 100),)
            elif reads == 2:
                context.now[0] = max(context.now[0], 100.18)
                if recovery == "epoch":
                    context.epoch[0] = 2
                packets = tuple(
                    source(i, 100 + i * 0.06, epoch=context.epoch[0]) for i in (1, 2, 3)
                )
                if recovery == "source_gap":
                    packets = packets[-1:]
            else:
                raise asyncio.CancelledError
            return SimpleNamespace(
                capture_epoch=context.epoch[0],
                resynchronized=reads == 2 and recovery == "resynchronized",
                packets=packets,
            )

    context.track.handle = Handle()
    await until_end(context.track)
    expected = [0, 1, 2, 3] if recovery == "continuous" else [0, 3]
    assert [timestamp // 2880 for timestamp, _epoch in context.calls] == expected
    assert context.track.sent_packets == len(expected) * 3
    sequences = [struct.unpack("!H", data[2:4])[0] for _time, data in context.sent]
    assert all((b - a) & 65535 == 1 for a, b in zip(sequences, sequences[1:], strict=False))
    timestamps = [struct.unpack("!I", data[4:8])[0] for _time, data in context.sent]
    assert all(
        0 < (b - a) & 0xFFFFFFFF < 2**31 for a, b in zip(timestamps, timestamps[1:], strict=False)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_during_close", [False, True])
async def test_expired_prefix_discards_old_batch_before_one_restart(
    monkeypatch, revoke_during_close
):
    context = setup(monkeypatch, cold=0.01)
    reads = 0
    failures = []
    context.track._failure = failures.append
    original_close = context.track._adapter.close

    async def close():
        await original_close()
        if revoke_during_close:
            context.allowed[0] = False

    context.track._adapter.close = close

    class Handle:
        async def read_audio(self, *, timeout):
            nonlocal reads
            reads += 1
            if reads == 1:
                packets = (source(0, 100),)
            elif reads == 2:
                context.now[0] = 100.7
                # Both are pre-recovery history. Even its newest packet has
                # only10ms freshness budget: never cold-start the worker on it.
                packets = (source(1, 100.21), source(2, 100.21))
            elif reads == 3:
                packets = (source(3, context.now[0]),)
            else:
                raise asyncio.CancelledError
            return SimpleNamespace(capture_epoch=1, resynchronized=False, packets=packets)

    context.track.handle = Handle()
    if revoke_during_close:
        try:
            await context.track._send_audio()
        finally:
            await context.track.close()
        assert failures == ["audio_output_failed"]
        assert len(context.factories) == 1
        assert [timestamp for timestamp, _epoch in context.calls] == [0]
        assert len(context.sent) == 3
    else:
        await until_end(context.track)
        assert len(context.factories) == 2
        assert [timestamp // 2880 for timestamp, _epoch in context.calls] == [0, 3]
        assert context.track._timing.values["adapter_recreates"] == 1
        assert len(context.sent) == 6
        assert failures == []
