"""Bounded complete audio ownership and separate conversion/egress tasks."""

import asyncio
import time
from types import SimpleNamespace

import pytest
from test_homekit_audio_live_edge import drain, setup, source
from test_homekit_audio_timing import make_track

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.homekit_audio import MAX_RETAINED_OUTPUT_BYTES, _AudioTiming
from halo_server.media.homekit_audio_codec import AudioAdapter, AudioTransformError
from halo_server.media.opus import AudioConfiguration, AudioOutput


def outputs(first, count=16):
    # Deliberately maximum payloads test bytes as well as packet count. These
    # framing-only tests never decode these synthetic placeholder bytes.
    return tuple(AudioOutput(b"x" * 1178, first + i * 960, 960, "adapt") for i in range(count))


def batch(*packets, epoch=1):
    return SimpleNamespace(capture_epoch=epoch, packets=packets, resynchronized=False)


@pytest.mark.asyncio
async def test_native_staging_queue_and_inflight_share_32_packet_40k_budget(monkeypatch):
    context = setup(monkeypatch, cold=0)
    track = context.track
    try:
        await track._audio_outputs(outputs(0), source(0, 100))
        await track._reserve_output_capacity()
        await track._audio_outputs(outputs(15360), source(1, 100))
        assert len(track._outputs) == 32 and track._output_bytes == 32 * 1178
        metrics = track.diagnostics()["metrics"]["maxima"]
        assert metrics["retained_output_packets"] == 32
        assert metrics["retained_output_bytes"] == 32 * 1178 + 2400
        assert metrics["retained_output_bytes"] < MAX_RETAINED_OUTPUT_BYTES

        # A pacing head stays charged across the synchronous fence. It is
        # released only by the sender finally, rather than hidden in a local.
        head = track._sending = track._outputs[0]
        track.fence()
        assert len(track._outputs) == 1 and track._output_bytes == 1178
        await track._send_output(head)
        assert not track._outputs and track._output_bytes == 0
        values = track.diagnostics()["metrics"]["counters"]
        assert values["output_retired_samples"] == 32 * 960
        assert values["output_sent_packets"] == 0
    finally:
        await track.close()


@pytest.mark.asyncio
async def test_response_capacity_wait_rechecks_privacy_and_retains_queue_owner(monkeypatch):
    context = setup(monkeypatch, cold=0)
    track = context.track
    waiting = None
    try:
        await track._audio_outputs(outputs(0), source(0, 100))
        await track._audio_outputs(outputs(15360), source(1, 100))
        waiting = asyncio.create_task(track._reserve_output_capacity())
        # asyncio.wait has a real scheduling point even though this fixture
        # replaces sleep to drive a deterministic media clock.
        done, _pending = await asyncio.wait({waiting}, timeout=0.01)
        assert not done and len(track._outputs) == 32
        context.allowed[0] = False
        track._output_space.set()
        with pytest.raises(HomeKitOperationError) as error:
            await waiting
        assert error.value.code == "policy_denied"
        assert len(track._outputs) == 32 and not context.sent
    finally:
        if waiting is not None:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        await track.close()


@pytest.mark.asyncio
async def test_each_complete_output_is_classified_before_input_frontier_advances(monkeypatch):
    context = setup(monkeypatch, cold=0)
    track = context.track
    track._input_spans.extend(((0, 960, 99), (960, 2880, 100)))
    try:
        await track._audio_outputs(
            tuple(AudioOutput(b"\xd8\x01", i * 960, 960, "adapt") for i in range(3)),
            source(0, 100),
        )
        await drain(track)
        values = track.diagnostics()["metrics"]["counters"]
        assert values["output_generated_samples"] == 2880
        assert values["output_stale_samples"] == 960
        assert values["output_sent_samples"] == 1920
        assert values["output_unvisited_samples"] == values["output_unfinished_samples"] == 0
        assert len(context.sent) == 2 and not track._input_spans
    finally:
        await track.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["privacy", "epoch", "stop"])
async def test_fresh_batch_cannot_cross_retirement_fence(monkeypatch, change):
    context = setup(monkeypatch, cold=0)
    track = context.track
    track._capture_epoch, track._live_edge_pending = 1, False
    track._input_spans.append((0, 2880, 99))
    entered, release = asyncio.Event(), asyncio.Event()
    close = track._adapter.close

    async def delayed_close():
        entered.set()
        await release.wait()
        await close()

    track._adapter.close = delayed_close
    pending = asyncio.create_task(track._audio_batch(batch(source(1, 100))))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if change == "privacy":
            context.allowed[0] = False
        elif change == "epoch":
            context.epoch[0] = 2
        else:
            track.fence()
        release.set()
        if change == "epoch":
            await pending
        else:
            with pytest.raises(HomeKitOperationError):
                await pending
        assert not context.calls and not context.sent and not track._outputs
        assert len(context.factories) == (2 if change == "epoch" else 1)
    finally:
        release.set()
        await asyncio.gather(pending, return_exceptions=True)
        await track.close()


@pytest.mark.asyncio
async def test_conversion_proceeds_while_egress_is_waiting_and_stop_joins_both(monkeypatch):
    track = make_track(monkeypatch, [0])
    track._clock = time.monotonic
    track.config = AudioConfiguration(24000, 20, "vbr", 24)
    track._timing = _AudioTiming(track.config, time.monotonic())
    send_entered, release, converted = asyncio.Event(), asyncio.Event(), asyncio.Event()
    feeds, sent = [], []
    closed = []

    class Adapter:
        async def feed(self, _payload, timestamp, *, capture_epoch):
            feeds.append(timestamp)
            if len(feeds) == 2:
                converted.set()
            await asyncio.sleep(0)
            return tuple(
                AudioOutput(b"\xd8\x01", timestamp + step, 960, "adapt") for step in (0, 960, 1920)
            )

        async def close(self):
            closed.append(True)

    class Handle:
        reads = 0

        async def read_audio(self, *, timeout):
            self.reads += 1
            if self.reads > 2:
                await asyncio.Event().wait()
            return batch(source(self.reads - 1, time.monotonic()))

    async def send(_data, **options):
        send_entered.set()
        await release.wait()
        if track._retired:
            return False
        assert time.monotonic() <= options["fresh_until"]
        sent.append(True)
        return True

    track._adapter, track.handle = Adapter(), Handle()
    track._send = send
    supervisor = asyncio.create_task(track._send_audio())
    try:
        await asyncio.wait_for(send_entered.wait(), 1)
        await asyncio.wait_for(converted.wait(), 1)
        assert len(feeds) == 2 and not sent
        assert track._sending is track._outputs[0]
        assert len(track._outputs) <= 6
        # Stop during a blocked send leaves no orphan conversion, sender, or
        # queued packet, and cannot let the blocked datagram escape later.
        track.fence()
        await track.close()
        with pytest.raises(asyncio.CancelledError):
            await supervisor
        release.set()
        assert not sent and closed == [True]
        assert all(task.done() for task in track._output_tasks)
        assert not track._outputs and track._output_bytes == 0
    finally:
        release.set()
        supervisor.cancel()
        await asyncio.gather(supervisor, return_exceptions=True)
        await track.close()


@pytest.mark.asyncio
async def test_adapter_native_response_reservation_covers_pending_inputs():
    adapter = AudioAdapter(AudioConfiguration(24000, 20, "vbr", 24))
    calls = []

    class Worker:
        async def feed(self, _payload, timestamp, *, max_outputs):
            calls.append(max_outputs)
            return tuple((timestamp + i * 960, 960, b"\xd8\x01") for i in range(max_outputs))

        async def close(self):
            pass

    adapter._worker = Worker()
    try:
        with pytest.raises(AudioTransformError, match="capacity"):
            await adapter._adapt(((b"\x58\x01", 0), (b"\x58\x01", 2880)))
        assert calls == [16]
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_brief_same_epoch_permission_toggle_retires_pcm_and_old_source(monkeypatch):
    context = setup(monkeypatch, cold=0)
    track = context.track
    try:
        await track._audio_batch(batch(source(0, 100)))
        assert len(track._outputs) == 3
        old = track._adapter
        # Denial seen by the sender must synchronously fence all queued output,
        # even if the producer is currently waiting for a source batch.
        context.allowed[0] = False
        await track._send_output(track._outputs[0])
        assert track._permission_paused and not track._outputs and not context.sent
        # Permission returns within10ms, far below the500ms freshness limit,
        # and the capture epoch remains identical. Old PCM still cannot survive.
        context.now[0] += 0.01
        context.allowed[0] = True
        await track._audio_batch(batch(source(1, 100.005)))
        assert old.closed and len(context.factories) == 2
        assert context.calls == [(0, 1)] and not track._outputs
        context.now[0] += 0.01
        await track._audio_batch(batch(source(2, context.now[0])))
        await drain(track)
        assert context.calls == [(0, 1), (5760, 1)] and len(context.sent) == 3
        values = track.diagnostics()["metrics"]["counters"]
        assert values["permission_pauses"] == 1
        assert values["source_authority_discarded_samples"] == 2880
        assert values["output_send_fenced_samples"] == 2880
    finally:
        await track.close()


@pytest.mark.asyncio
async def test_denied_permission_closes_native_once_without_busy_recreation(monkeypatch):
    context = setup(monkeypatch, cold=0)
    track = context.track
    calls = []
    old_close = track._adapter.close

    async def close():
        calls.append(True)
        await old_close()

    track._adapter.close = close
    context.allowed[0] = False
    try:
        for _ in range(5):
            assert not await track._ready_audio()
        assert calls == [True] and len(context.factories) == 1
        assert track._adapter is None and track._permission_paused
    finally:
        await track.close()


@pytest.mark.asyncio
async def test_permission_return_cannot_reauthorize_rest_of_same_native_response(monkeypatch):
    context = setup(monkeypatch, cold=0)
    track = context.track
    checks = 0

    def transient_denial(_permission):
        nonlocal checks
        checks += 1
        if checks == 1:
            raise HomeKitOperationError("policy_denied")

    track._check = transient_denial
    try:
        await track._audio_outputs(outputs(0, 3), source(0, 100))
        assert track._permission_paused and not track._outputs
        assert track.diagnostics()["metrics"]["counters"]["output_send_fenced_samples"] == 2880
        assert not context.sent
    finally:
        await track.close()


@pytest.mark.asyncio
async def test_source_read_observed_denial_latches_privacy_even_if_checks_already_allow(
    monkeypatch,
):
    context = setup(monkeypatch, cold=0)
    track = context.track
    await track._audio_batch(batch(source(0, 100)))
    old = track._adapter

    class Handle:
        reads = 0

        async def read_audio(self, *, timeout):
            self.reads += 1
            if self.reads == 1:
                raise HomeKitOperationError("policy_denied")
            if self.reads == 2:
                assert old.closed and len(context.factories) == 2
                return batch(source(1, context.now[0]))
            raise asyncio.CancelledError

    track.handle = Handle()
    try:
        with pytest.raises(asyncio.CancelledError):
            await track._produce_audio()
        await drain(track)
        assert context.calls == [(0, 1), (2880, 1)]
        assert len(context.sent) == 3 and old.closed
        values = track.diagnostics()["metrics"]["counters"]
        assert values["permission_pauses"] == 1
        assert values["output_send_fenced_samples"] == 2880
    finally:
        await track.close()
