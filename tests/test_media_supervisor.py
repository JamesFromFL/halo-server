import asyncio
import threading

import pytest

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
