"""Native work, source fences and actual retirement remain separate owners."""

import asyncio
import gc
import threading
import time
import weakref
from fractions import Fraction
from types import SimpleNamespace

import av
import numpy as np
import pytest
from test_media_session import FakeClient, _receiver_kwargs, fake_supervisor

from halo_server.media import legacy_audio
from halo_server.media import session as session_module
from halo_server.media.audio import AudioNormalizationError, NormalizedOpusPacket
from halo_server.media.leases import LeasePurpose
from halo_server.media.session import CameraMediaSession, MediaSessionState


def frame(pts=0, samples=960):
    value = av.AudioFrame.from_ndarray(
        np.zeros((1, samples * 2), dtype=np.int16), format="s16", layout="stereo"
    )
    value.pts, value.time_base, value.sample_rate = pts, Fraction(1, 48000), 48000
    return value


class Encoder:
    def __init__(self):
        self.timestamp = 0

    def encode(self, payload, channels, rate):
        result = tuple(
            NormalizedOpusPacket(b"\xf8\xff\xfe", self.timestamp + offset)
            for offset in range(0, len(payload) * 48000 // (channels * 2 * rate), 960)
        )
        self.timestamp += len(payload) * 48000 // (channels * 2 * rate)
        return result


def make_session(factory=Encoder, callback=None):
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="camera"),
        receiver_supervisor=fake_supervisor,
        audio_encoder_factory=factory,
        on_raw_audio=callback,
    )
    lease = session.acquire(LeasePurpose.HOMEKIT_LIVE)
    return session, client, lease


def audio_packets(session):
    # Audio is retained before a video IDR; the public snapshot intentionally
    # exposes only decodable A/V windows. Inspect actual admitted audio here.
    with session.buffer._condition:
        return tuple(packet for packet in session.buffer._packets if packet.stream == "audio")


async def until(predicate, timeout=2):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["initialization", "encode"])
async def test_native_work_does_not_block_source_authority_or_ring_loop(blocked):
    entered, release = threading.Event(), threading.Event()
    native_threads, callbacks = [], []
    ring_thread = threading.get_ident()

    class Slow(Encoder):
        def __init__(self):
            native_threads.append(threading.get_ident())
            if blocked == "initialization":
                entered.set()
                assert release.wait(3)
            super().__init__()

        def encode(self, *args):
            if blocked == "encode":
                entered.set()
                assert release.wait(3)
            return super().encode(*args)

        def close(self):
            native_threads.append(threading.get_ident())

    session, client, lease = make_session(Slow, lambda *_args: callbacks.append(True))
    task = asyncio.create_task(session._decoded_audio(1, frame()))
    stopper = None
    try:
        await until(entered.is_set)
        assert session.is_lease_active(lease.id)
        assert session.source_clock_snapshot() == ()
        original = _receiver_kwargs(client.futures[0])["on_encoded_audio"]
        assert original(b"\xf8\xff\xfe", 1234)
        assert session.original_audio_buffer.snapshot().packets[0].rtp_timestamp == 1234
        for _ in range(20):
            await asyncio.sleep(0)
        assert not task.done() and not callbacks
        stopper = asyncio.create_task(asyncio.to_thread(session.stop_all))
        await until(lambda: session._stop is not None and session._stop.is_set())
        assert not session.receiver_retired and not stopper.done()
        assert not session.is_lease_active(lease.id)
        release.set()
        await task
        await stopper
        assert session.receiver_retired and not callbacks
        assert session.buffer.snapshot().packets == ()
        assert all(identifier != ring_thread for identifier in native_threads)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        if stopper is not None:
            await stopper
        await asyncio.to_thread(session.stop_all)


@pytest.mark.asyncio
async def test_raw_callback_affinity_and_native_close_are_preserved():
    native_threads, raw_threads, state_owned = [], [], []

    class Observed(Encoder):
        def __init__(self):
            native_threads.append(threading.get_ident())
            super().__init__()

        def encode(self, *args):
            native_threads.append(threading.get_ident())
            return super().encode(*args)

        def close(self):
            native_threads.append(threading.get_ident())

    def raw(*_args):
        raw_threads.append(threading.get_ident())
        state_owned.append(session._lock._is_owned())
        assert session.is_lease_active(lease.id)

    session, _client, lease = make_session(Observed, raw)
    try:
        await session._decoded_audio(1, frame())
        assert len(audio_packets(session)) == 1
        assert raw_threads == [threading.get_ident()] and state_owned == [False]
    finally:
        await asyncio.to_thread(session.stop_all)
    assert len(native_threads) == 3
    assert all(identifier != threading.get_ident() for identifier in native_threads)


@pytest.mark.asyncio
async def test_async_initialization_failure_keeps_stable_code_and_no_false_codec_ready():
    calls = []

    def fail():
        calls.append(threading.get_ident())
        raise RuntimeError("not exported")

    session, _client, _lease = make_session(fail)
    try:
        assert not calls and session.snapshot().state is MediaSessionState.STARTING
        await session._decoded_audio(1, frame())
        assert session.snapshot().failure == "audio_encoder_initialization_failed"
        assert session.snapshot().opus_frame_duration_ms is None
        assert session.snapshot().lease_count == 1
        assert calls != [threading.get_ident()]
    finally:
        await asyncio.to_thread(session.stop_all)


@pytest.mark.asyncio
async def test_unexpected_receiver_completion_fences_callbacks_before_native_retirement():
    entered, release = threading.Event(), threading.Event()
    callbacks = []

    class Slow(Encoder):
        def encode(self, *args):
            entered.set()
            assert release.wait(3)
            return super().encode(*args)

    session, client, _lease = make_session(Slow, lambda *_args: callbacks.append(True))
    future = client.futures[0]
    encoded_audio = _receiver_kwargs(future)["on_encoded_audio"]
    stop = session._stop
    task = asyncio.create_task(session._decoded_audio(1, frame()))
    try:
        await until(entered.is_set)
        future.complete_unexpectedly()
        assert stop.is_set()
        assert session.snapshot().state is MediaSessionState.STOPPING
        assert session._retiring_audio and not session.receiver_retired
        assert not encoded_audio(b"\xf8\xff\xfe", 1234)
        assert session.original_audio_buffer.snapshot().packets == ()
        release.set()
        await task
        await until(lambda: not session._retiring_audio)
        assert not callbacks and not audio_packets(session)
        assert session.snapshot().state is MediaSessionState.FAILED
        assert session.snapshot().failure == "receiver_stopped"
        assert len(client.futures) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(session.stop_all)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_retired_constructor_result_cannot_publish_or_fail_replacement(fail):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def construct():
        calls.append(True)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            if fail:
                raise RuntimeError("old constructor error")
        return Encoder()

    session, client, _lease = make_session(construct)
    task = asyncio.create_task(session._decoded_audio(1, frame()))
    stopper = None
    try:
        await until(entered.is_set)
        stopper = asyncio.create_task(asyncio.to_thread(session.stop_all))
        await until(lambda: session._retiring_audio)
        release.set()
        await asyncio.gather(task, stopper)
        assert session.snapshot().state is MediaSessionState.IDLE
        assert session.snapshot().failure is None
        session.acquire(LeasePurpose.HOMEKIT_LIVE)
        assert len(client.futures) == 2
        await session._decoded_audio(2, frame())
        assert len(audio_packets(session)) == 1
        assert session.snapshot().failure is None and not session._stop.is_set()
    finally:
        release.set()
        await asyncio.gather(
            task, *(tuple([stopper]) if stopper is not None else ()), return_exceptions=True
        )
        await asyncio.to_thread(session.stop_all)


@pytest.mark.asyncio
async def test_native_timeout_quarantines_then_replacement_waits_for_actual_retirement(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Slow(Encoder):
        def encode(self, *args):
            calls.append(True)
            entered.set()
            assert release.wait(3)
            return super().encode(*args)

    monkeypatch.setattr(session_module, "STOP_TIMEOUT", 0.03)
    session, client, _lease = make_session(Slow)
    task = asyncio.create_task(session._decoded_audio(1, frame()))
    try:
        await until(entered.is_set)
        await asyncio.to_thread(session.stop_all)
        assert session.snapshot().state is MediaSessionState.STOPPING
        assert session.snapshot().failure == "receiver_stop_timeout"
        assert not session.receiver_retired and session._retiring_audio
        session.acquire(LeasePurpose.HOMEKIT_LIVE)
        assert len(client.futures) == 1
        release.set()
        await task
        await until(lambda: len(client.futures) == 2)
        assert not session.buffer.snapshot().packets
        assert len(calls) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(session.stop_all)


@pytest.mark.asyncio
async def test_failed_native_close_retained_then_exact_retry_releases(monkeypatch):
    closed = []

    class CloseFailsOnce(Encoder):
        def close(self):
            closed.append(threading.get_ident())
            if len(closed) == 1:
                raise RuntimeError("injected native close failure")

    monkeypatch.setattr(session_module, "STOP_TIMEOUT", 0.03)
    session, _client, _lease = make_session(CloseFailsOnce)
    await session._decoded_audio(1, frame())
    await asyncio.to_thread(session.stop_all)
    assert not session.receiver_retired
    assert len(closed) == 1 and len(session._retiring_audio) == 1
    await asyncio.to_thread(session.stop_all)
    assert session.receiver_retired and len(closed) == 2


@pytest.mark.asyncio
async def test_discontinuity_resets_encoder_and_preserves_source_missing_duration():
    creates, closes = [], []

    class Observed(Encoder):
        def __init__(self):
            creates.append(True)
            super().__init__()

        def close(self):
            closes.append(True)

    session, _client, _lease = make_session(Observed)
    epoch = session.buffer.capture_epoch
    try:
        await session._decoded_audio(1, frame(0))
        await session._decoded_audio(1, frame(4800))
        assert [packet.rtp_timestamp for packet in audio_packets(session)] == [0, 4800]
        assert session.buffer.capture_epoch == epoch
        assert len(creates) == 2 and len(closes) == 1
        assert session.diagnostics()["metrics"]["counters"]["legacy_discontinuities"] == 1
    finally:
        await asyncio.to_thread(session.stop_all)


@pytest.mark.asyncio
async def test_unknown_decoded_timing_is_explicit_legacy_failure_only():
    session, client, lease = make_session()
    value = frame()
    value.pts = None
    try:
        await session._decoded_audio(1, value)
        assert session.diagnostics()["legacy_audio"]["failure"] == "legacy_audio_timing_unavailable"
        assert not session._stop.is_set() and session.is_lease_active(lease.id)
        assert _receiver_kwargs(client.futures[0])["on_encoded_audio"](b"\xf8\xff\xfe", 100)
        assert session.original_audio_buffer.snapshot().packets
        assert not session.buffer.snapshot().packets
    finally:
        await asyncio.to_thread(session.stop_all)


def test_external_callback_reentrant_stop_fences_without_self_join():
    durations = []

    def raw(*_args):
        began = time.monotonic()
        session.stop_all()
        durations.append(time.monotonic() - began)
        assert not session.receiver_retired

    session, _client, _lease = make_session(callback=raw)
    try:
        session._raw_audio(1, bytes(1920), 1, 48000)
        assert len(durations) == 1 and durations[0] < 0.1
        assert session.snapshot().failure == "receiver_cleanup_pending"
    finally:
        session.stop_all()
    assert session.receiver_retired


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [True, False])
async def test_two_busy_workers_bound_and_cancel_third_queued_source(monkeypatch, cancel):
    entered = [threading.Event(), threading.Event()]
    release = threading.Event()
    constructed = []

    def factory(index):
        def construct():
            constructed.append(index)
            if index < 2:
                entered[index].set()
                assert release.wait(4)
            return Encoder()

        return construct

    monkeypatch.setattr(legacy_audio, "NATIVE_QUEUE_SECONDS", 0.03)
    sessions = [make_session(factory(index)) for index in range(3)]
    tasks = [
        asyncio.create_task(sessions[index][0]._decoded_audio(1, frame())) for index in range(2)
    ]
    third = None
    try:
        await until(lambda: all(event.is_set() for event in entered))

        class QueuedFrame:
            pass

        queued = QueuedFrame()
        reference = weakref.ref(queued)
        third = asyncio.create_task(sessions[2][0]._decoded_audio(1, queued))
        del queued
        await until(lambda: sessions[2][0]._audio_owner._job is not None)
        s3, c3, lease = sessions[2]
        assert s3.is_lease_active(lease.id) and s3.source_clock_snapshot() == ()
        assert _receiver_kwargs(c3.futures[0])["on_encoded_audio"](b"\xf8\xff\xfe", 0)
        if cancel:
            third.cancel()
            with pytest.raises(asyncio.CancelledError):
                await third
        else:
            await third
            assert s3.diagnostics()["legacy_audio"]["failure"] == "legacy_audio_capacity"
        assert constructed == [0, 1] or constructed == [1, 0]
        gc.collect()
        assert reference() is None  # Not hidden in a queued native Future/closure.
        assert s3._audio_owner._job is None
        await asyncio.to_thread(s3.stop_all)
        assert s3.receiver_retired
    finally:
        for session, _client, _lease in sessions[:2]:
            with session._lock:
                session._stop.set()
                session._retire_audio_locked()
        release.set()
        await asyncio.gather(
            *tasks, *([third] if third is not None else []), return_exceptions=True
        )
        for session, _client, _lease in sessions:
            await asyncio.to_thread(session.stop_all)


@pytest.mark.asyncio
async def test_same_runner_attempt_waits_for_old_owner_and_rejects_old_error():
    entered, release = threading.Event(), threading.Event()

    class Old(Encoder):
        def encode(self, *_args):
            entered.set()
            assert release.wait(3)
            raise AudioNormalizationError("old native failure")

    session, _client, _lease = make_session(Old)
    task = asyncio.create_task(session._decoded_audio(1, frame()))
    try:
        await until(entered.is_set)
        session._attempt_ended(1, 1, None)
        with pytest.raises(AudioNormalizationError, match="cleanup pending"):
            session._attempt_started(1, 2)
        release.set()
        await task
        await until(lambda: not session._retiring_audio)
        session._audio_encoder_factory = Encoder
        session._attempt_started(1, 2)
        await session._decoded_audio(1, frame())
        assert not session._stop.is_set()
        assert session.snapshot().failure is None
        assert session.snapshot().capture_attempt == 2
        assert len(audio_packets(session)) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.to_thread(session.stop_all)


def test_native_encoder_reentrant_stop_cannot_wait_for_its_own_thread():
    durations = []

    class Reentrant(Encoder):
        def encode(self, *args):
            began = time.monotonic()
            session.stop_all()
            durations.append(time.monotonic() - began)
            return super().encode(*args)

    session, _client, _lease = make_session(Reentrant)
    try:
        session._raw_audio(1, bytes(1920), 1, 48000)
        assert durations and durations[0] < 0.1
        assert not audio_packets(session)
    finally:
        session.stop_all()
    assert session.receiver_retired


@pytest.mark.asyncio
async def test_actual_native_120ms_normalization_and_shared_consumer_release():
    session, client, live = make_session(session_module.NormalizedOpusEncoder)
    owner = session._audio_owner
    try:
        legacy = session.acquire(LeasePurpose.SCRYPTED_LIVE)
        await session._decoded_audio(1, frame(0, 5760))
        packets = audio_packets(session)
        assert len(packets) == 6
        assert [packet.rtp_timestamp for packet in packets] == list(range(0, 5760, 960))
        decoder = av.CodecContext.create("libopus", "r")
        decoder.sample_rate, decoder.layout, decoder.format = 48000, "mono", "s16"
        decoded = [
            value for packet in packets for value in decoder.decode(av.Packet(packet.payload))
        ]
        assert len(decoded) == 6 and all(value.samples == 960 for value in decoded)
        assert all(len(value.layout.channels) == 1 for value in decoded)
        session.release(legacy.id)
        assert session.is_lease_active(live.id) and len(client.futures) == 1
        assert not owner.closed
    finally:
        await asyncio.to_thread(session.stop_all)
    assert owner.closed and session.receiver_retired
