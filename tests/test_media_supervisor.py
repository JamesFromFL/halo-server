import asyncio
import threading

import pytest

from halo_server.media.ring_media import MediaCompatibilityError
from halo_server.media.supervisor import supervise_receiver


@pytest.mark.asyncio
async def test_receiver_supervisor_restarts_after_failure():
    stop = threading.Event()
    attempts = 0

    class Receiver:
        def __init__(self, *_args, **_kwargs):
            pass

        async def receive(self, _stop):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("first failure")
            stop.set()

    await supervise_receiver(
        object(),
        None,
        None,
        stop,
        receiver_factory=Receiver,
        retry_min=0,
        retry_max=0,
    )

    assert attempts == 2


@pytest.mark.asyncio
async def test_receiver_supervisor_restarts_after_natural_end():
    stop = threading.Event()
    attempts = 0

    class Receiver:
        def __init__(self, *_args, **_kwargs):
            pass

        async def receive(self, _stop):
            nonlocal attempts
            attempts += 1
            if attempts == 2:
                stop.set()

    await supervise_receiver(
        object(),
        None,
        None,
        stop,
        receiver_factory=Receiver,
        retry_min=0,
        retry_max=0,
    )

    assert attempts == 2


@pytest.mark.asyncio
async def test_receiver_supervisor_resets_backoff_after_delivering_media(monkeypatch):
    stop = threading.Event()
    attempts = 0
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    class Receiver:
        def __init__(self, _device, _video, _audio, *, on_encoded_video, **_kwargs):
            self.on_encoded_video = on_encoded_video

        async def receive(self, _stop):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("failure before media")
            if attempts == 2:
                self.on_encoded_video(b"frame", 1)
                raise RuntimeError("failure after media")
            stop.set()

    await supervise_receiver(
        object(),
        None,
        None,
        stop,
        receiver_factory=Receiver,
        receiver_kwargs={"on_encoded_video": lambda *_args: None},
        retry_min=1,
        retry_max=8,
    )

    assert attempts == 3
    assert sleeps.count(0.1) == 20


@pytest.mark.asyncio
async def test_receiver_supervisor_propagates_cancellation():
    stop = threading.Event()

    class Receiver:
        def __init__(self, *_args, **_kwargs):
            pass

        async def receive(self, _stop):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await supervise_receiver(
            object(),
            None,
            None,
            stop,
            receiver_factory=Receiver,
            retry_min=0,
            retry_max=0,
        )


@pytest.mark.asyncio
async def test_receiver_supervisor_fences_old_attempt_callbacks_and_publishes_boundaries():
    stop = threading.Event()
    calls = []
    boundaries = []
    saved = []

    class Receiver:
        def __init__(self, _device, _video, audio, *, on_encoded_video, **_kwargs):
            self.video = on_encoded_video
            self.audio = audio

        async def receive(self, _stop):
            saved.append((self.video, self.audio))
            if len(saved) == 1:
                self.video(b"first", 1)
                raise RuntimeError("restart")
            assert saved[0][0](b"stale", 2) is False
            assert saved[0][1](b"stale", 1, 48000) is False
            self.video(b"second", 3)
            stop.set()

    await supervise_receiver(
        object(),
        None,
        lambda *_args: calls.append("audio"),
        stop,
        receiver_factory=Receiver,
        receiver_kwargs={
            "on_encoded_video": lambda payload, _stamp: calls.append(payload),
            "on_attempt": lambda attempt: boundaries.append(("start", attempt)),
            "on_attempt_end": lambda attempt, _error: boundaries.append(("end", attempt)),
        },
        retry_min=0,
        retry_max=0,
    )
    assert calls == [b"first", b"second"]
    assert boundaries == [("start", 1), ("end", 1), ("start", 2), ("end", 2)]


@pytest.mark.asyncio
async def test_receiver_supervisor_does_not_retry_permanent_abi_error():
    attempts = []

    class Receiver:
        def __init__(self, *_args, **_kwargs):
            attempts.append(True)

        async def receive(self, _stop):
            raise MediaCompatibilityError("unsupported ABI")

    with pytest.raises(MediaCompatibilityError):
        await supervise_receiver(
            object(),
            None,
            None,
            threading.Event(),
            receiver_factory=Receiver,
            retry_min=0,
            retry_max=0,
        )
    assert attempts == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("video_result", [False, None])
async def test_audio_or_rejected_video_cannot_reset_retry_backoff(monkeypatch, video_result):
    stop = threading.Event()
    sleeps = []
    attempts = 0

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", sleep)

    class Receiver:
        def __init__(self, _device, _video, audio, *, on_encoded_video):
            self.audio = audio
            self.video = on_encoded_video

        async def receive(self, _stop):
            nonlocal attempts
            attempts += 1
            if attempts == 3:
                stop.set()
                return
            self.audio(b"audio", 1, 48000)
            if video_result is False:
                self.video(b"rejected", 1)
            raise RuntimeError("no useful video")

    await supervise_receiver(
        object(),
        None,
        lambda *_args: None,
        stop,
        receiver_factory=Receiver,
        receiver_kwargs={"on_encoded_video": lambda *_args: video_result},
        retry_min=0.1,
        retry_max=0.8,
    )
    assert sleeps == [0.1, 0.1, 0.1]
