"""One owned sampler, distinct packet/frame boundaries and sanitized status."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from test_media_ingress_diagnostics import session_fixture

from halo_server.media.receiver_metrics import ReceiverPipeline
from halo_server.media.ring_media import observe_receiver_statistics


@pytest.mark.asyncio
async def test_existing_sampler_wakes_faster_than_public_getstats_without_extra_owner():
    calls, wakes, snapshots = [], [], []
    stop, terminal = threading.Event(), asyncio.Event()

    class Receiver:
        def __init__(self, kind):
            self.kind = kind

        async def getStats(self):
            calls.append((self.kind, asyncio.get_running_loop().time()))
            return {}

    class Pipeline:
        def wake_lag(self, seconds):
            wakes.append(seconds)

        def snapshot(self):
            return {"fixed": True}

    def sampled(value, _lag, _elapsed):
        snapshots.append(value)
        if len(snapshots) == 2:
            stop.set()

    await observe_receiver_statistics(
        Receiver("audio"),
        Receiver("video"),
        stop,
        terminal,
        sampled,
        interval=0.05,
        wake_interval=0.01,
        pipeline=Pipeline(),
    )
    assert [kind for kind, _at in calls] == ["audio", "video", "audio", "video"]
    assert calls[2][1] - calls[0][1] >= 0.045
    assert len(wakes) >= 8
    assert all(value["pipeline"] == {"fixed": True} for value in snapshots)


@pytest.mark.asyncio
async def test_sampler_cancellation_joins_its_current_public_stats_call():
    entered, joined = asyncio.Event(), asyncio.Event()

    class Receiver:
        async def getStats(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                joined.set()

    snapshots = []
    task = asyncio.create_task(
        observe_receiver_statistics(
            Receiver(),
            Receiver(),
            threading.Event(),
            asyncio.Event(),
            lambda *args: snapshots.append(args),
            interval=0.001,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert joined.is_set() and not snapshots


def test_pipeline_status_whitelist_and_retirement_revision():
    session, _client, _lease, callbacks = session_fixture()
    pipeline = ReceiverPipeline(SimpleNamespace(), SimpleNamespace())
    pipeline.frame("audio")
    pipeline.wake_lag(0.01)
    snapshot = pipeline.snapshot()
    snapshot["secret"] = "private-value"
    snapshot["bins"][0]["payload"] = "private-value"
    snapshot["metrics"]["secret"] = "private-value"
    callbacks["on_receiver_stats"]({"pipeline": snapshot}, 0, 0)
    try:
        diagnostics = session.diagnostics()
        assert diagnostics["receiver_pipeline"]["capacity"] == 64
        assert diagnostics["receiver_stats_kind"] == "public_nonaggregate"
        assert "private-value" not in json.dumps(diagnostics)
    finally:
        session.stop_all()
    callbacks["on_receiver_stats"]({"pipeline": snapshot}, 0, 0)
    assert session.diagnostics()["receiver_pipeline"] is None
