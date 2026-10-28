"""Original Opus and source clock reads retain selected privacy authority."""

import asyncio
import threading
from dataclasses import replace

import pytest
from test_homekit_controller import configured

from halo_server.homekit.models import HomeKitOperationError
from halo_server.media.opus import OriginalOpusBuffer
from halo_server.media.ring_media import SourceClockObservation

OPUS = b"\xf8\xff\xfe"  # Valid 20ms mono Opus silence.


@pytest.mark.asyncio
async def test_original_audio_has_independent_cursor_and_epoch_from_video():
    controller, backend, boot, (scope,) = await configured()
    handle = await controller.open_live(boot, scope)
    resource = backend.resources[0]
    audio = resource.original_audio_buffer = OriginalOpusBuffer()
    resource.source_clock_callback = lambda: ()
    try:
        packet = audio.append_audio(OPUS, 0)
        assert (await handle.read(timeout=0)).packets
        assert (await handle.read_audio(timeout=0)).packets == (packet,)
        assert not (await handle.read_audio(timeout=0)).packets
        resource.buffer.clear()
        # An old audio batch cannot cross the shared video capture attempt.
        audio.append_audio(OPUS, 960)
        assert not (await handle.read_audio(timeout=0)).packets
        audio.clear(capture_epoch=resource.buffer.capture_epoch)
        current = audio.append_audio(OPUS, 0)
        batch = await handle.read_audio(timeout=0)
        assert batch.packets == (current,)
        assert batch.capture_epoch == resource.buffer.capture_epoch
    finally:
        await controller.detach()


@pytest.mark.asyncio
async def test_read_audio_rechecks_privacy_after_blocked_buffer_read(monkeypatch):
    controller, backend, boot, (scope,) = await configured()
    handle = await controller.open_live(boot, scope)
    resource = backend.resources[0]
    audio = resource.original_audio_buffer = OriginalOpusBuffer()
    audio.append_audio(OPUS, 0)
    entered, release = threading.Event(), threading.Event()
    original = audio.read

    def blocked(**kwargs):
        entered.set()
        assert release.wait(3)
        return original(**kwargs)

    monkeypatch.setattr(audio, "read", blocked)
    reading = asyncio.create_task(handle.read_audio(timeout=0))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        with pytest.raises(HomeKitOperationError) as busy:
            await handle.read_audio(timeout=0)
        assert busy.value.code == "busy"
        controller.fence_camera(boot, scope)
        release.set()
        with pytest.raises(HomeKitOperationError):
            await asyncio.wait_for(reading, 2)
    finally:
        release.set()
        await asyncio.gather(reading, return_exceptions=True)
        await controller.detach()


@pytest.mark.asyncio
async def test_source_clock_snapshot_filters_old_attempt_and_rechecks_callback_authority():
    controller, backend, boot, (scope,) = await configured()
    handle = await controller.open_live(boot, scope)
    resource = backend.resources[0]
    clock = SourceClockObservation("audio", 123, 0, 1 << 32, 0, resource.buffer.capture_epoch)
    resource.source_clock_callback = lambda: (replace(clock, capture_epoch=0), clock)
    try:
        assert handle.source_clocks == (clock,)

        def stale_during_read():
            controller.fence_camera(boot, scope)
            return (clock,)

        resource.source_clock_callback = stale_during_read
        with pytest.raises(HomeKitOperationError):
            _ = handle.source_clocks
    finally:
        await controller.detach()
