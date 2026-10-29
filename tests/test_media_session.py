import concurrent.futures
import logging
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from halo_server.media import session as media_session
from halo_server.media.leases import LeasePurpose, MediaLeaseCoordinator
from halo_server.media.session import CameraMediaSession, MediaSessionState

SPS_PPS_IDR = (
    b"\x00\x00\x00\x01\x67\x64\x00\x32\x00\x00\x00\x01\x68\xee\x3c\x80\x00\x00\x00\x01\x65\x88\x84"
)


class FakeFuture:
    def __init__(self, coroutine):
        self.coroutine = coroutine
        self.callbacks = []
        self.cancelled = False
        self.finished = False

    def add_done_callback(self, callback):
        self.callbacks.append(callback)

    def done(self):
        return self.finished

    def result(self, *, timeout):
        assert timeout > 0
        self.finished = True
        self.coroutine.close()
        for callback in self.callbacks:
            callback(self)

    def cancel(self):
        self.cancelled = True
        self.finished = True
        self.coroutine.close()

    def complete_unexpectedly(self):
        self.finished = True
        self.coroutine.close()
        for callback in self.callbacks:
            callback(self)


class FakeClient:
    def __init__(self):
        self.futures = []

    def submit(self, coroutine):
        future = FakeFuture(coroutine)
        self.futures.append(future)
        return future


class FailingClient:
    def __init__(self):
        self.coroutine = None

    def submit(self, coroutine):
        self.coroutine = coroutine
        raise RuntimeError("loop unavailable")


class FakeClock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value


async def fake_supervisor(
    _device,
    _on_video,
    _on_audio,
    _stop,
    *,
    receiver_kwargs,
):
    del receiver_kwargs


def _session(client=None):
    client = client or FakeClient()
    device = SimpleNamespace(id=42, name="Front Door")
    return (
        CameraMediaSession(
            client,
            device,
            receiver_supervisor=fake_supervisor,
        ),
        client,
    )


def _receiver_kwargs(future):
    return future.coroutine.cr_frame.f_locals["receiver_kwargs"]


def test_multiple_leases_submit_exactly_one_ring_receiver():
    session, client = _session()

    event = session.acquire(LeasePurpose.EVENT)
    live = session.acquire(LeasePurpose.HOMEKIT_LIVE)

    assert len(client.futures) == 1
    assert event.generation == live.generation == 1
    assert session.snapshot().lease_count == 2
    assert session.snapshot().state is MediaSessionState.STARTING
    session.stop_all()


def test_session_source_identity_does_not_accept_same_api_id_replacement():
    client = FakeClient()
    device = SimpleNamespace(id=42, name="Original")
    session = CameraMediaSession(client, device, receiver_supervisor=fake_supervisor)
    assert session.owns_source(client, device)
    assert not session.owns_source(client, SimpleNamespace(id=42, name="Replacement"))
    assert not session.owns_source(FakeClient(), device)


def test_fast_lease_check_rejects_expiry_without_tick_and_release_without_renewal():
    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Camera"),
        clock=clock,
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
    )
    lease = session.acquire(LeasePurpose.SNAPSHOT, ttl=1)
    assert session.is_lease_active(lease.id)
    clock.value = 1
    assert not session.is_lease_active(lease.id)
    assert session.snapshot().lease_count == 1  # The fast check has no expiration side effects.
    session.release(lease.id)
    fresh = session.acquire(LeasePurpose.EVENT)
    assert session.is_lease_active(fresh.id)
    assert not session.is_lease_active(lease.id)
    session.release(fresh.id)
    assert not session.is_lease_active(fresh.id)


def test_last_release_stops_receiver_but_first_release_does_not():
    session, client = _session()
    event = session.acquire(LeasePurpose.EVENT)
    live = session.acquire(LeasePurpose.HOMEKIT_LIVE)
    future = client.futures[0]
    stop = future.coroutine.cr_frame.f_locals["_stop"]

    session.release(event.id)
    assert stop.is_set() is False
    assert future.finished is False

    session.release(live.id)
    assert stop.is_set() is True
    assert future.finished is True
    assert session.snapshot().state is MediaSessionState.IDLE


def test_encoded_keyframe_marks_buffer_and_session_ready():
    session, client = _session()
    session.acquire(LeasePurpose.EVENT)
    callbacks = _receiver_kwargs(client.futures[0])

    on_audio = client.futures[0].coroutine.cr_frame.f_locals["_on_audio"]
    on_audio(np.zeros((960, 2), dtype="<i2").tobytes(), 2, 48_000)
    callbacks["on_encoded_video"](SPS_PPS_IDR, 200)

    snapshot = session.snapshot()
    assert snapshot.state is MediaSessionState.READY
    assert snapshot.buffer_ready is True
    assert snapshot.buffer_packets == 1
    assert snapshot.opus_frame_duration_ms == 20
    session.stop_all()


def test_stale_callbacks_cannot_write_into_new_generation():
    session, client = _session()
    first = session.acquire(LeasePurpose.SNAPSHOT)
    stale_callbacks = _receiver_kwargs(client.futures[0])
    session.release(first.id)
    second = session.acquire(LeasePurpose.EVENT)

    stale_callbacks["on_encoded_video"](SPS_PPS_IDR, 100)

    assert first.generation == 1
    assert second.generation == 2
    assert session.snapshot().buffer_ready is False
    session.stop_all()


def test_incompatible_codec_stops_retry_loop_and_marks_failure():
    session, client = _session()
    session.acquire(LeasePurpose.EVENT)
    future = client.futures[0]
    callbacks = _receiver_kwargs(future)
    stop = future.coroutine.cr_frame.f_locals["_stop"]

    callbacks["on_encoded_incompatible"]("audio", "audio/pcmu")

    assert stop.is_set() is True
    assert session.snapshot().state is MediaSessionState.FAILED
    assert session.snapshot().failure == "unsupported_audio_codec"
    session.stop_all()


def test_unexpected_receiver_completion_does_not_clear_active_lease():
    session, client = _session()
    session.acquire(LeasePurpose.EVENT)

    client.futures[0].complete_unexpectedly()

    assert session.snapshot().lease_count == 1
    assert session.snapshot().state is MediaSessionState.FAILED
    assert session.snapshot().failure == "receiver_stopped"
    session.stop_all()


def test_failed_receiver_restarts_same_lease_after_backoff():
    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Front Door"),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        clock=clock,
        retry_initial=1,
        retry_max=4,
    )
    lease = session.acquire(LeasePurpose.EVENT, lease_id="event")

    client.futures[0].complete_unexpectedly()
    clock.value = 0.9
    session.expire(now=clock.value)
    assert len(client.futures) == 1

    clock.value = 1
    session.expire(now=clock.value)

    assert len(client.futures) == 2
    assert session.snapshot().state is MediaSessionState.STARTING
    assert session.snapshot().generation == 2
    assert session.snapshot().lease_count == 1
    assert session.renew(lease.id, now=clock.value).generation == 2
    _receiver_kwargs(client.futures[1])["on_encoded_video"](SPS_PPS_IDR, 200)
    assert session.snapshot().state is MediaSessionState.READY
    session.stop_all()


def test_failed_intercom_receiver_rearms_speaker_gate_before_retry():
    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Front Door"),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        clock=clock,
        retry_initial=1,
        retry_max=2,
    )
    lease = session.acquire(LeasePurpose.INTERCOM, lease_id="talk")
    source = _receiver_kwargs(client.futures[0])["microphone_source"]
    payload = b"\x01\x00" * 960
    session.push_intercom_audio(lease.id, payload)
    generation = source.snapshot().intercom_generation
    assert source.set_speaker_ready(generation, True)
    assert source.pull() == payload

    client.futures[0].complete_unexpectedly()
    session.push_intercom_audio(lease.id, payload)

    assert source.snapshot().intercom_active is True
    assert source.snapshot().speaker_ready is False
    assert source.pull() is None

    clock.value = 1
    session.expire(now=clock.value)
    assert session.snapshot().generation == 2
    assert source.snapshot().intercom_active is True
    assert source.snapshot().speaker_ready is False
    session.stop_all()


def test_repeated_receiver_failures_use_bounded_exponential_backoff():
    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Front Door"),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        clock=clock,
        retry_initial=1,
        retry_max=2,
    )
    session.acquire(LeasePurpose.EVENT)
    client.futures[0].complete_unexpectedly()

    clock.value = 1
    session.expire(now=clock.value)
    client.futures[1].complete_unexpectedly()
    clock.value = 2.9
    session.expire(now=clock.value)
    assert len(client.futures) == 2

    clock.value = 3
    session.expire(now=clock.value)
    client.futures[2].complete_unexpectedly()
    clock.value = 4.9
    session.expire(now=clock.value)
    assert len(client.futures) == 3

    clock.value = 5
    session.expire(now=clock.value)
    assert len(client.futures) == 4
    session.stop_all()


def test_additional_lease_during_backoff_does_not_start_overlapping_receiver():
    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Front Door"),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        clock=clock,
        retry_initial=1,
        retry_max=2,
    )
    session.acquire(LeasePurpose.EVENT)
    client.futures[0].complete_unexpectedly()

    clock.value = 0.5
    session.acquire(LeasePurpose.HOMEKIT_LIVE, now=clock.value)
    assert len(client.futures) == 1

    clock.value = 1
    session.expire(now=clock.value)
    assert len(client.futures) == 2
    assert session.snapshot().lease_count == 2
    session.stop_all()


def test_acquire_at_retry_deadline_returns_new_receiver_generation():
    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Front Door"),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        clock=clock,
        retry_initial=1,
        retry_max=2,
    )
    session.acquire(LeasePurpose.EVENT, lease_id="event")
    client.futures[0].complete_unexpectedly()

    clock.value = 1
    lease = session.acquire(
        LeasePurpose.HOMEKIT_LIVE,
        lease_id="live",
        now=clock.value,
    )

    assert lease.generation == 2
    assert session.snapshot().generation == 2
    session.stop_all()


def test_renew_at_retry_deadline_returns_new_receiver_generation():
    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Front Door"),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        clock=clock,
        retry_initial=1,
        retry_max=2,
    )
    session.acquire(LeasePurpose.EVENT, lease_id="event")
    client.futures[0].complete_unexpectedly()

    clock.value = 1
    lease = session.renew("event", now=clock.value)

    assert lease is not None
    assert lease.generation == 2
    assert session.snapshot().generation == 2
    session.stop_all()


def test_retry_submission_failure_remains_retryable_with_same_lease():
    class FailsSecondSubmissionClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.submission_attempts = 0

        def submit(self, coroutine):
            self.submission_attempts += 1
            if self.submission_attempts == 2:
                coroutine.close()
                raise RuntimeError("injected retry submission failure")
            return super().submit(coroutine)

    clock = FakeClock()
    client = FailsSecondSubmissionClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Front Door"),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        clock=clock,
        retry_initial=1,
        retry_max=2,
    )
    session.acquire(LeasePurpose.EVENT)
    client.futures[0].complete_unexpectedly()

    clock.value = 1
    session.expire(now=clock.value)
    assert session.snapshot().state is MediaSessionState.FAILED
    assert session.snapshot().failure == "receiver_submission_failed"
    assert session.snapshot().lease_count == 1

    clock.value = 3
    session.expire(now=clock.value)
    assert client.submission_attempts == 3
    assert len(client.futures) == 2
    assert session.snapshot().state is MediaSessionState.STARTING
    session.stop_all()


def test_retry_audio_encoder_failure_remains_retryable_with_same_lease():
    class FakeEncoder:
        def encode(self, _payload, _channels, _rate):
            return []

    encoder_attempts = 0

    def encoder_factory():
        nonlocal encoder_attempts
        encoder_attempts += 1
        if encoder_attempts == 2:
            raise RuntimeError("injected encoder initialization failure")
        return FakeEncoder()

    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Front Door"),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        audio_encoder_factory=encoder_factory,
        clock=clock,
        retry_initial=1,
        retry_max=2,
    )
    session.acquire(LeasePurpose.EVENT)
    assert encoder_attempts == 0
    session._raw_audio(1, b"\x00\x00", 1, 48000)
    assert encoder_attempts == 1
    client.futures[0].complete_unexpectedly()
    with session._condition:
        assert session._condition.wait_for(lambda: not session._retiring_audio, timeout=1)

    clock.value = 1
    session.expire(now=clock.value)
    assert session.snapshot().state is MediaSessionState.STARTING
    session._raw_audio(2, b"\x00\x00", 1, 48000)
    assert session.snapshot().state is MediaSessionState.FAILED
    assert session.snapshot().failure == "audio_encoder_initialization_failed"
    assert session.snapshot().lease_count == 1
    client.futures[1].complete_unexpectedly()
    with session._condition:
        assert session._condition.wait_for(lambda: not session._retiring_audio, timeout=1)

    clock.value = 3
    session.expire(now=clock.value)
    session._raw_audio(3, b"\x00\x00", 1, 48000)
    assert encoder_attempts == 3
    assert len(client.futures) == 3
    assert session.snapshot().state is MediaSessionState.STARTING
    session.stop_all()


def test_releasing_last_lease_cancels_failed_receiver_retry():
    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Front Door"),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        clock=clock,
        retry_initial=1,
        retry_max=2,
    )
    lease = session.acquire(LeasePurpose.EVENT)
    client.futures[0].complete_unexpectedly()

    session.release(lease.id, now=0)
    clock.value = 2
    session.expire(now=clock.value)

    assert len(client.futures) == 1
    assert session.snapshot().state is MediaSessionState.IDLE
    assert session.snapshot().lease_count == 0


def test_receiver_submission_failure_closes_coroutine_and_marks_failure():
    client = FailingClient()
    session, _ = _session(client)

    with pytest.raises(RuntimeError, match="loop unavailable"):
        session.acquire(LeasePurpose.EVENT)

    assert client.coroutine.cr_frame is None
    assert session.snapshot().state is MediaSessionState.FAILED
    assert session.snapshot().failure == "receiver_submission_failed"
    assert session.snapshot().lease_count == 0
    assert session._audio_encoder is None


def test_raw_audio_callback_is_generation_guarded():
    delivered = []
    client = FakeClient()
    device = SimpleNamespace(id=42, name="Front Door")
    session = CameraMediaSession(
        client,
        device,
        receiver_supervisor=fake_supervisor,
        on_raw_audio=lambda payload, channels, rate: delivered.append((payload, channels, rate)),
    )
    lease = session.acquire(LeasePurpose.EVENT)
    future = client.futures[0]
    on_audio = future.coroutine.cr_frame.f_locals["_on_audio"]

    raw = np.zeros(960, dtype="<i2").tobytes()
    on_audio(raw, 1, 48000)
    session.release(lease.id)
    on_audio(raw, 1, 48000)

    assert delivered == [(raw, 1, 48000)]


def test_intercom_audio_requires_matching_active_lease_and_reaches_receiver_source(caplog):
    caplog.set_level(logging.INFO)
    session, client = _session()
    lease = session.acquire(LeasePurpose.INTERCOM, lease_id="talk")
    source = _receiver_kwargs(client.futures[0])["microphone_source"]
    payload = b"\x01\x00" * 960

    with pytest.raises(RuntimeError, match="not active"):
        session.push_intercom_audio("other", payload)
    session.push_intercom_audio(lease.id, payload)

    generation = source.snapshot().intercom_generation
    assert session.intercom_generation(lease.id) == generation
    assert source.pull() is None
    assert source.set_speaker_ready(generation, True)
    assert session.wait_intercom_ready(lease.id, 0.1) is True
    assert source.pull() == payload
    session.release(lease.id)
    assert "accepted=1 consumed=1 dropped=0 queued=0" in caplog.text
    with pytest.raises(RuntimeError, match="not active"):
        session.push_intercom_audio(lease.id, payload)
    with pytest.raises(RuntimeError, match="not active"):
        session.push_intercom_generation(generation, payload)
    assert session.intercom_generation(lease.id) is None
    assert session.wait_intercom_ready(lease.id, 0.1) is False


def test_releasing_intercom_clears_queued_audio_while_live_stream_continues():
    session, client = _session()
    live = session.acquire(LeasePurpose.SCRYPTED_LIVE, lease_id="live")
    talk = session.acquire(LeasePurpose.INTERCOM, lease_id="talk")
    source = _receiver_kwargs(client.futures[0])["microphone_source"]
    session.push_intercom_audio(talk.id, b"\x01\x00" * 960)

    session.release(talk.id)

    assert source.pull() is None
    assert session.snapshot().lease_count == 1
    session.release(live.id)


def test_invalid_raw_audio_stops_session_with_stable_failure():
    session, client = _session()
    session.acquire(LeasePurpose.EVENT)
    future = client.futures[0]
    on_audio = future.coroutine.cr_frame.f_locals["_on_audio"]
    stop = future.coroutine.cr_frame.f_locals["_stop"]

    on_audio(b"misaligned", 2, 48_000)

    assert stop.is_set() is True
    assert session.snapshot().state is MediaSessionState.FAILED
    assert session.snapshot().failure == "audio_normalization_failed"
    session.stop_all()


def test_rejected_encoded_video_stops_and_retries_receiver():
    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Front Door"),
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
        clock=clock,
        retry_initial=1,
        retry_max=2,
    )
    session.acquire(LeasePurpose.EVENT)
    future = client.futures[0]
    stop = future.coroutine.cr_frame.f_locals["_stop"]

    _receiver_kwargs(future)["on_encoded_video"](b"", 100)

    assert stop.is_set() is True
    assert session.snapshot().state is MediaSessionState.FAILED
    assert session.snapshot().failure == "encoded_video_rejected"
    future.complete_unexpectedly()
    clock.value = 1
    session.expire(now=clock.value)
    assert len(client.futures) == 2
    session.stop_all()


def test_stop_wait_occurs_outside_session_state_lock():
    entered = threading.Event()
    finish = threading.Event()

    class BlockingFuture(FakeFuture):
        def result(self, *, timeout):
            entered.set()
            assert finish.wait(timeout)
            return super().result(timeout=timeout)

    class BlockingClient(FakeClient):
        def submit(self, coroutine):
            future = BlockingFuture(coroutine)
            self.futures.append(future)
            return future

    session, client = _session(BlockingClient())
    lease = session.acquire(LeasePurpose.EVENT)
    release = threading.Thread(target=session.release, args=(lease.id,))
    release.start()
    assert entered.wait(0.5)

    assert session.snapshot().state is MediaSessionState.STOPPING

    finish.set()
    release.join(timeout=0.5)
    assert not release.is_alive()
    assert client.futures[0].finished is True
    assert session.snapshot().state is MediaSessionState.IDLE


def test_stop_timeout_preserves_receiver_barrier(monkeypatch):
    monkeypatch.setattr(media_session, "STOP_TIMEOUT", 0.01)

    class TimeoutFuture(FakeFuture):
        def result(self, *, timeout):
            raise concurrent.futures.TimeoutError

    class TimeoutClient(FakeClient):
        def __init__(self):
            super().__init__()

        def submit(self, coroutine):
            future = TimeoutFuture(coroutine)
            self.futures.append(future)
            return future

    client = TimeoutClient()
    session, _ = _session(client)
    first = session.acquire(LeasePurpose.EVENT)
    _receiver_kwargs(client.futures[0])["on_encoded_video"](SPS_PPS_IDR, 100)
    assert session.snapshot().buffer_ready

    session.release(first.id)

    assert client.futures[0].cancelled is False
    assert session.snapshot().state is MediaSessionState.STOPPING
    assert session.snapshot().failure == "receiver_stop_timeout"
    assert not session.receiver_retired
    assert not session.snapshot().buffer_ready

    second = session.acquire(LeasePurpose.EVENT)
    assert second.generation == 2
    assert len(client.futures) == 1
    assert not session.snapshot().buffer_ready

    client.futures[0].complete_unexpectedly()
    assert len(client.futures) == 2
    assert session.snapshot().state is MediaSessionState.STARTING

    session.stop_all()
    client.futures[1].complete_unexpectedly()
    assert session.snapshot().state is MediaSessionState.IDLE
    assert session.receiver_retired


def test_capture_attempt_restart_clears_history_and_audio_encoder():
    session, client = _session()
    lease = session.acquire(LeasePurpose.EVENT)
    callbacks = _receiver_kwargs(client.futures[0])
    callbacks["on_encoded_video"](SPS_PPS_IDR, 200)
    first = session.snapshot()
    assert first.capture_attempt == 1
    assert first.buffer_ready
    callbacks["on_attempt_end"](1, RuntimeError("transient"))
    assert not session.snapshot().buffer_ready
    assert session.snapshot().state is MediaSessionState.FAILED
    callbacks["on_attempt"](2)
    restarted = session.snapshot()
    assert restarted.capture_attempt == 2
    assert restarted.generation == lease.generation
    assert restarted.state is MediaSessionState.STARTING
    assert callbacks["on_encoded_video"](b"\x00\x00\x01\x41p-only", 400) is False
    assert not session.snapshot().buffer_ready
    assert callbacks["on_encoded_video"](SPS_PPS_IDR, 500) is True
    assert session.snapshot().buffer_ready
    session.stop_all()
    assert session.receiver_retired


def test_incompatible_receiver_is_not_retried_by_session_expiry():
    clock = FakeClock()
    client = FakeClient()
    session = CameraMediaSession(
        client,
        SimpleNamespace(id=42, name="Camera"),
        clock=clock,
        lease_coordinator=MediaLeaseCoordinator(clock=clock),
        receiver_supervisor=fake_supervisor,
    )
    session.acquire(LeasePurpose.EVENT)
    _receiver_kwargs(client.futures[0])["on_encoded_incompatible"]("video", "video/vp8")
    client.futures[0].complete_unexpectedly()
    clock.value = 20
    session.expire()
    assert len(client.futures) == 1
    assert session.snapshot().failure == "unsupported_video_codec"
    assert not session.receiver_retired
    session.stop_all()
    assert session.receiver_retired
