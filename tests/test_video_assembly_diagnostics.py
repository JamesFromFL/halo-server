"""Bounded recovery observations reach session diagnostics without media identity."""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from test_media_ingress_diagnostics import session_fixture

from halo_server.media.ring_media import observe_receiver_statistics
from halo_server.media.session import _safe_video_assembly
from halo_server.media.video_jitter import H264JitterBuffer


def observation():
    return H264JitterBuffer().snapshot() | {
        "completed_aus": 12,
        "discarded_aus": 5,
        "loss_events": 1,
        "recovery_requests": 2,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("buffered_packets", 513),
        ("buffered_bytes", 1024 * 1024 + 1),
        ("completed_aus", True),
        ("discarded_aus", -1),
        ("loss_events", 2**53),
        ("reorder_timeouts", -1),
        ("assembly_timeouts", float("nan")),
        ("waiting_for_idr", "yes"),
    ],
)
def test_invalid_assembly_diagnostics_fail_closed(field, value):
    assert _safe_video_assembly(observation() | {field: value}) is None


@pytest.mark.asyncio
async def test_assembly_snapshot_reaches_session_and_expires_and_clears_on_stop():
    session, _, _, callbacks = session_fixture()
    now = [10.0]
    session._diagnostic_clock = lambda: now[0]
    stop, terminal = threading.Event(), asyncio.Event()
    value = observation() | {"source_ssrc": 123, "private": "must-not-leak"}

    class Receiver:
        _RTCRtpReceiver__jitter_buffer = SimpleNamespace(snapshot=lambda: value)

        async def getStats(self):
            return {}

    def observed(snapshot, lag, duration):
        callbacks["on_receiver_stats"](snapshot, lag, duration)
        stop.set()

    try:
        await asyncio.wait_for(
            observe_receiver_statistics(
                Receiver(),
                Receiver(),
                stop,
                terminal,
                observed,
                interval=0.001,
                wake_interval=0.001,
            ),
            timeout=1,
        )
        result = session.diagnostics()
        assert result["video_assembly"] == observation()
        assert "must-not-leak" not in json.dumps(result)
        result["video_assembly"]["completed_aus"] = 999
        assert session.diagnostics()["video_assembly"]["completed_aus"] == 12
        now[0] += 3.1
        assert session.diagnostics()["video_assembly"] is None
        callbacks["on_receiver_stats"]({"video_assembly": value}, 0, 0)
        assert session.diagnostics()["video_assembly"] == observation()
        session.stop_all()
        callbacks["on_receiver_stats"]({"video_assembly": value}, 0, 0)
        assert session.diagnostics()["video_assembly"] is None
    finally:
        session.stop_all()
