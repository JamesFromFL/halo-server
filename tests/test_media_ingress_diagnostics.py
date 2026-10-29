"""Diagnostics observe ingress/waits without changing receipt or media authority."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from test_media_session import FakeClient, FakeClock, _receiver_kwargs, fake_supervisor

from halo_server.media import ring_media
from halo_server.media.leases import LeasePurpose
from halo_server.media.session import CameraMediaSession
from halo_server.media.supervisor import supervise_receiver
from halo_server.media.telemetry import safe_diagnostics


class Encoder:
    def encode(self, *_args):
        return ()


def session_fixture():
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=91, name="secret-camera"),
        receiver_supervisor=fake_supervisor,
        audio_encoder_factory=Encoder,
        clock=FakeClock(42),
    )
    lease = session.acquire(LeasePurpose.HOMEKIT_LIVE)
    return session, client, lease, _receiver_kwargs(client.futures[0])


def test_entry_precedes_source_observer_and_preserves_original_receipt(monkeypatch):
    session, _client, _lease, callbacks = session_fixture()
    now = [10.0]
    session._diagnostic_clock = lambda: now[0]
    monkeypatch.setattr(ring_media.time, "monotonic", lambda: now[0])

    def install(_receiver, _kind, _callback):
        def observe(_timestamp):
            assert session.diagnostics()["metrics"]["counters"]["audio_ingress_frames"] == 1
            assert not session.original_audio_buffer.snapshot().packets
            now[0] += 0.12

        return observe

    monkeypatch.setattr(ring_media, "install_source_clock_observer", install)
    deliver = ring_media._encoded_source_callback(
        object(),
        "audio",
        callbacks["on_encoded_audio"],
        lambda _value: None,
        callbacks["on_encoded_ingress"],
    )
    try:
        assert deliver(b"\xf8\xff\xfe", 12345) is True
        packet = session.original_audio_buffer.snapshot().packets[0]
        assert packet.received_at == 42
        assert not hasattr(session._ingress_local, "entry")
        diagnostics = session.diagnostics()
        counters, timings = diagnostics["metrics"]["counters"], diagnostics["metrics"]["timings"]
        assert counters["audio_admitted_frames"] == 1
        assert counters["audio_admitted_samples"] == 960
        assert timings["audio_pre_admission"]["max_us"] == 120000
        assert timings["audio_entry_to_admission"]["max_us"] == 120000
        assert "secret-camera" not in json.dumps(diagnostics)
    finally:
        session.stop_all()


@pytest.mark.parametrize("failure", ["observer", "media"])
def test_entry_token_clears_on_existing_observer_or_media_exception(monkeypatch, failure):
    session, _client, _lease, callbacks = session_fixture()

    def fail(_value):
        raise RuntimeError("existing-source-error")

    monkeypatch.setattr(ring_media, "install_source_clock_observer", lambda *_args: fail)
    callback = fail if failure == "media" else callbacks["on_encoded_audio"]
    if failure == "media":
        monkeypatch.setattr(
            ring_media, "install_source_clock_observer", lambda *_args: lambda _: None
        )

        def callback(*_args):
            fail(None)

    deliver = ring_media._encoded_source_callback(
        object(), "audio", callback, lambda _: None, callbacks["on_encoded_ingress"]
    )
    try:
        with pytest.raises(RuntimeError, match="existing-source-error"):
            deliver(b"\xf8\xff\xfe", 12345)
        assert not hasattr(session._ingress_local, "entry")
    finally:
        session.stop_all()


@pytest.mark.parametrize("invalid_clock", [False, True])
def test_optional_diagnostic_failure_does_not_interrupt_media(monkeypatch, invalid_clock):
    calls = []

    def failure(*_args):
        raise asyncio.CancelledError

    if invalid_clock:
        monkeypatch.setattr(ring_media.time, "monotonic", lambda: float("nan"))
    deliver = ring_media._encoded_source_callback(
        object(), "audio", lambda *args: calls.append(args), None, failure
    )
    deliver(b"media-stays-private", 42)
    assert calls == [(b"media-stays-private", 42)]


def test_metric_failure_does_not_reject_admitted_media():
    session, _client, _lease, callbacks = session_fixture()

    class FailedMetrics:
        def count(self, *_args):
            raise asyncio.CancelledError

        observe = count

        def snapshot(self):
            raise RuntimeError("diagnostic snapshot unavailable")

    session._metrics = FailedMetrics()
    try:
        assert callbacks["on_encoded_audio"](b"\xf8\xff\xfe", 100)
        assert session.original_audio_buffer.snapshot().packets[0].rtp_timestamp == 100
        assert safe_diagnostics(session) is None
    finally:
        session.stop_all()


def test_state_lock_wait_observed_without_blocking_diagnostics_snapshot():
    session, _client, lease, callbacks = session_fixture()
    held, release = threading.Event(), threading.Event()
    done = []
    threads = []

    def holder():
        with session._lock:
            held.set()
            assert release.wait(2)

    def call(function):
        function()
        done.append(True)

    owner = threading.Thread(target=holder)
    owner.start()
    assert held.wait(1)
    try:
        for function in (
            session.source_clock_snapshot,
            lambda: session.is_lease_active(lease.id),
            lambda: callbacks["on_encoded_audio"](b"\xf8\xff\xfe", 100),
        ):
            thread = threading.Thread(target=call, args=(function,))
            thread.start()
            threads.append(thread)
        started = time.monotonic()
        assert session.diagnostics()["receiver_stats"] is None
        assert time.monotonic() - started < 0.1
        time.sleep(0.04)
    finally:
        release.set()
        owner.join(2)
        for thread in threads:
            thread.join(2)
        session.stop_all()
    assert not owner.is_alive() and all(not thread.is_alive() for thread in threads)
    assert len(done) == 3
    timings = session.diagnostics()["metrics"]["timings"]
    assert all(
        timings[name]["max_us"] >= 20000
        for name in ("source_clock_read_wait", "authority_wait", "audio_admission_wait")
    )


def test_legacy_encode_duration_is_observed_outside_state_lock():
    session, _client, _lease, _callbacks = session_fixture()
    encoded_under_lock = []

    class SlowEncoder:
        def encode(self, *_args):
            encoded_under_lock.append(session._lock._is_owned())
            time.sleep(0.025)
            return ()

    session._audio_owner.factory = SlowEncoder
    try:
        session._raw_audio(1, b"\x00\x00", 1, 48000)
        assert encoded_under_lock == [False]
        assert session.diagnostics()["metrics"]["timings"]["legacy_encode"]["max_us"] >= 20000
    finally:
        session.stop_all()


def stats_row(kind, **values):
    return SimpleNamespace(
        type="inbound-rtp",
        kind=kind,
        id="secret-stats-id",
        ssrc=123,
        transportId="secret-transport",
        packetsReceived=values.get("received", 5),
        packetsLost=values.get("lost", -2),
        jitter=values.get("jitter", 48),
    )


def test_public_receiver_statistics_preserve_signed_loss_and_unknowns():
    result = ring_media._inbound_statistics({"secret": stats_row("audio")}, "audio")
    assert result == {
        "packets_received": 5,
        "packets_lost": -2,
        "jitter_ticks": 48,
        "clock_rate": 48000,
    }
    assert ring_media._inbound_statistics({}, "audio") is None
    assert (
        ring_media._inbound_statistics({"a": stats_row("audio"), "b": stats_row("audio")}, "audio")
        is None
    )
    malformed = ring_media._inbound_statistics(
        {"a": stats_row("video", received=-1, lost=True, jitter="secret")}, "video"
    )
    assert malformed == {
        "packets_received": None,
        "packets_lost": None,
        "jitter_ticks": None,
        "clock_rate": 90000,
    }
    assert "secret" not in json.dumps(result)


def test_receiver_stats_expire_and_failed_refresh_does_not_invent_zero():
    session, _client, _lease, callbacks = session_fixture()
    now = [10.0]
    session._diagnostic_clock = lambda: now[0]
    row = ring_media._inbound_statistics({"x": stats_row("audio")}, "audio")
    try:
        callbacks["on_receiver_stats"]({"audio": row, "video": None, "secret": "omit"}, 0.01, 0.02)
        row["packets_received"] = 999
        assert session.diagnostics()["receiver_stats"]["audio"]["packets_received"] == 5
        now[0] += 3.1
        assert session.diagnostics()["receiver_stats"] is None
        callbacks["on_receiver_stats"](None, 0.03, 0.25)
        assert session.diagnostics()["receiver_stats"] is None
        assert session.diagnostics()["metrics"]["counters"]["receiver_stats_missing"] == 1
    finally:
        session.stop_all()


@pytest.mark.asyncio
async def test_statistics_timeout_and_callback_failure_are_nonfatal_and_owned():
    stop, terminal = threading.Event(), asyncio.Event()
    retired, notifications = [], []

    class Receiver:
        async def getStats(self):
            try:
                await asyncio.Event().wait()
            finally:
                retired.append(True)

    def callback(snapshot, lag, duration):
        notifications.append((snapshot, lag, duration))
        stop.set()
        raise asyncio.CancelledError

    await asyncio.wait_for(
        ring_media.observe_receiver_statistics(
            Receiver(), Receiver(), stop, terminal, callback, interval=0.001, timeout=0.02
        ),
        1,
    )
    assert len(retired) == 1
    assert len(notifications) == 1 and notifications[0][0] is None
    assert notifications[0][1] >= 0 and notifications[0][2] >= 0.015


@pytest.mark.asyncio
async def test_statistics_cancel_joins_active_public_api_call():
    entered = asyncio.Event()
    retired = []

    class Receiver:
        async def getStats(self):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                retired.append(True)

    task = asyncio.create_task(
        ring_media.observe_receiver_statistics(
            Receiver(),
            Receiver(),
            threading.Event(),
            asyncio.Event(),
            lambda *_args: None,
            interval=0.001,
        )
    )
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert retired == [True]


@pytest.mark.asyncio
async def test_supervisor_attempt_fences_new_telemetry_callbacks():
    stop = threading.Event()
    saved, calls = [], []

    class Receiver:
        def __init__(self, *_args, **kwargs):
            saved.append(kwargs)

        async def receive(self, _stop):
            if len(saved) == 1:
                raise RuntimeError("retry-this-owned-attempt")
            for name in ("on_encoded_ingress", "on_receiver_stats"):
                assert saved[0][name](None, None, None) is False
                saved[1][name](None, None, None)
            stop.set()

    await supervise_receiver(
        SimpleNamespace(name="test"),
        None,
        None,
        stop,
        receiver_factory=Receiver,
        receiver_kwargs={
            name: lambda *_args, name=name: calls.append(name)
            for name in ("on_encoded_ingress", "on_receiver_stats")
        },
        retry_min=0.001,
        retry_max=0.001,
    )
    assert calls == ["on_encoded_ingress", "on_receiver_stats"]


@pytest.mark.asyncio
async def test_statistics_success_exports_only_public_numeric_rows():
    stop = threading.Event()
    captured = []

    class Receiver:
        def __init__(self, kind):
            self.kind = kind

        async def getStats(self):
            return {"secret-id": stats_row(self.kind)}

    def callback(snapshot, lag, duration):
        captured.append(snapshot)
        assert lag >= 0 and duration >= 0
        stop.set()

    await asyncio.wait_for(
        ring_media.observe_receiver_statistics(
            Receiver("audio"), Receiver("video"), stop, asyncio.Event(), callback, interval=0.001
        ),
        1,
    )
    assert len(captured) == 1
    assert captured[0]["audio"]["packets_lost"] == -2
    assert captured[0]["video"]["clock_rate"] == 90000
    assert "secret" not in json.dumps(captured)


def test_old_receiver_statistics_cannot_reappear_after_stop():
    session, _client, _lease, callbacks = session_fixture()
    entered, release = threading.Event(), threading.Event()

    class PausedSnapshot(dict):
        def get(self, name, default=None):
            if name == "audio":
                entered.set()
                assert release.wait(2)
            return super().get(name, default)

    snapshot = PausedSnapshot(audio={"packets_received": 5}, video=None)
    thread = threading.Thread(target=lambda: callbacks["on_receiver_stats"](snapshot, 0, 0))
    thread.start()
    assert entered.wait(1)
    try:
        session.stop_all()
        assert session.snapshot().state == "idle"
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    assert session.diagnostics()["receiver_stats"] is None
    callbacks["on_receiver_stats"]({"audio": {"packets_received": 6}}, 0, 0)
    assert session.diagnostics()["receiver_stats"] is None


def test_ingress_before_reset_is_not_correlated_with_new_admission():
    session, _client, _lease, callbacks = session_fixture()
    cleanup = callbacks["on_encoded_ingress"]("audio", 100, time.monotonic())
    assert session._ingress_entry(1, "audio", 100) is not None
    try:
        with session._lock:
            session._clear_media_locked()
        assert session._ingress_entry(1, "audio", 100) is None
        assert session._last_ingress == session._last_pts == {}
    finally:
        cleanup()
        session.stop_all()
    assert not hasattr(session._ingress_local, "entry")


def test_ingress_waiting_before_diagnostics_lock_cannot_repopulate_after_stop():
    session, _client, _lease, callbacks = session_fixture()
    entered, release = threading.Event(), threading.Event()
    inner = session._diagnostic_lock
    caller = None
    results = []

    class PausedLock:
        def __enter__(self):
            if threading.current_thread() is caller:
                entered.set()
                assert release.wait(2)
            inner.acquire()

        def __exit__(self, *_args):
            inner.release()

    session._diagnostic_lock = PausedLock()
    caller = threading.Thread(
        target=lambda: results.append(
            callbacks["on_encoded_ingress"]("audio", 100, time.monotonic())
        )
    )
    caller.start()
    assert entered.wait(1)
    try:
        session.stop_all()
    finally:
        release.set()
        caller.join(2)
    assert not caller.is_alive() and results == [None]
    assert session._last_ingress == session._last_pts == {}
